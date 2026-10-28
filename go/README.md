# OTel Helper — Go

OpenTelemetry instrumentation helper library for Go services. A single call configures tracing, metrics, and logging following best practices.

📖 **[HOW-TO.md](HOW-TO.md)** — Developer guide (HTTP, gRPC, workers, metrics, logs)
🚀 **[example/](example/)** — Sample apps with distributed traces

## Quick Start

```go
shutdown, err := otelhelper.Setup(ctx)
if err != nil { log.Fatal(err) }
defer shutdown(ctx)
```

## Installation

```bash
go get github.com/staffops/staffops-otel-libs/go
```

Requires Go 1.26+.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_SERVICE_NAME` | _(unset)_ | Service name. Order: code > `OTEL_SERVICE_NAME` > `service.name` in `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` > SDK `unknown_service:<exe>` (+ warning) |
| `SERVICE_NAME` | _(unset)_ | Deprecated alias, lowest priority; warns once when set |
| `ENVIRONMENT` | _(unset)_ | Deprecated alias for `deployment.environment.name` (`OTEL_RESOURCE_ATTRIBUTES` wins). Unset → attribute absent + warning; values other than LOCAL/DEV/HML/PRD are emitted verbatim |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost` | Collector endpoint |
| `OTEL_EXPORTER_OTLP_INSECURE` | _(unset)_ | `true` = plaintext for **schemeless** endpoints. With `http://`/`https://` the scheme decides; a contradicting value is warned and ignored |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode: DEBUG logs, attribute `debug=true` on entry spans |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | _(unset)_ | No effect in Go (removed in 0.3.0); warns when set. Import the `ext/*` modules instead |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0-1.0). 1.0 = AlwaysOn |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Prometheus `/metrics` port when no OTLP endpoint is configured |

> These variables are injected automatically by infrastructure. Application teams **do not need to configure them manually**.

> ⚠️ **`OTEL_HELPER_DEBUG_LEVEL=true` in production causes cost explosion and backend saturation.** Use only for targeted troubleshooting.

---

## API Reference

### `Setup(ctx, ...Option) (Shutdown, error)`

Configures tracing, metrics, and logging. Call once at startup. Returns a `Shutdown` function for deferred cleanup. Thread-safe (uses `sync.Mutex`; returns `noopShutdown` on error so callers can retry with valid config).

```go
shutdown, err := otelhelper.Setup(ctx,
    otelhelper.WithServiceName("my-api"),
    otelhelper.WithEnvironment(otelhelper.PRD),
)
```

### `GetTracer(name ...string) trace.Tracer`

Returns a Tracer from the global provider. Defaults to `"otel-helper"` if no name provided.

```go
tracer := otelhelper.GetTracer("my-service")
ctx, span := tracer.Start(ctx, "operation-name")
defer span.End()
```

### `GetMeter(name ...string) metric.Meter`

Returns a Meter from the global provider. Defaults to `"otel-helper"` if no name provided.

```go
meter := otelhelper.GetMeter("my-service")
counter, _ := meter.Int64Counter("requests.total")
counter.Add(ctx, 1)
```

### `StartRootSpan(ctx, tracer, name, ...SpanStartOption) (context.Context, trace.Span)`

Starts a new span detached from any parent (new trace). Use in workers where each iteration should be an independent trace.

```go
ctx, span := otelhelper.StartRootSpan(ctx, tracer, "process-batch")
defer span.End()
```

### `NewHTTPHandler(handler http.Handler, operation string) http.Handler`

Wraps an `http.Handler` with OTel tracing and HTTP server metrics. Every request is measured, health checks included; health-check spans are dropped by the sampler `Setup` installs (see [Health checks](#health-checks-spans-dropped-metrics-kept)).

Server spans are named `<METHOD> <route>` from the `http.ServeMux` pattern (`GET /orders/{id}`, with `http.route` set), whether the
handler wraps one route or the whole mux; without a pattern the name is just the method (`GET`). Since 0.3.0 the `operation` argument
is ignored (kept for source compatibility).

```go
mux.Handle("POST /orders", otelhelper.NewHTTPHandler(handler, "orders")) // span name: "POST /orders"
```

### `NewHTTPTransport(base http.RoundTripper) http.RoundTripper`

Wraps an `http.RoundTripper` with OTel tracing for **outgoing** HTTP requests. Requests to a health path (same list and matching as the server side) get no client span (nor client metric). Pass `nil` to wrap `http.DefaultTransport`.

```go
client := &http.Client{Transport: otelhelper.NewHTTPTransport(nil)}
resp, err := client.Do(req) // automatically creates client spans with context propagation
```

### `NewSlogHandler() slog.Handler`

Returns the helper's `slog.Handler`, deciding per record: while `Setup` has a logs exporter installed, records go through the OTel bridge (`otelslog`; records logged with a span context carry `trace_id`/`span_id`); otherwise (before `Setup`, after `Shutdown`, no endpoint, `OTEL_LOGS_EXPORTER=none`) they are written to stdout as one JSON line (`time`, `level`, `msg`, attributes, `exception` for the first error-valued attribute) at the environment's level. Trace correlation needs the `*Context` slog calls (`InfoContext`, …). A handler created before `Setup` switches when `Setup` runs.

```go
handler := otelhelper.NewSlogHandler()
logger := slog.New(handler)
```

### `DefaultLogLevel(env DeploymentEnvironment, debug bool) slog.Level`

Returns the appropriate `slog.Level` for a given environment: LOCAL=Debug, DEV/HML=Info, PRD=Warning. Debug override forces Debug.

### `NewLogger(env DeploymentEnvironment, debug bool) *slog.Logger`

Returns a `*slog.Logger` with the environment's level over the same per-record handler as `NewSlogHandler` (OTel bridge while a logs exporter is installed, stdout JSON otherwise).

```go
logger := otelhelper.NewLogger(otelhelper.PRD, false) // Warning level, OTel bridge
slog.SetDefault(logger)
```

### gRPC (stats handlers)

| Function | Description |
|----------|-------------|
| `GRPCServerOption()` | `grpc.ServerOption` installing the server stats handler (unary + streaming) |
| `GRPCDialOption()` | `grpc.DialOption` installing the client stats handler (unary + streaming) |
| `GRPCServerHandler()` | The server `stats.Handler` itself (for `grpc.StatsHandler`) |
| `GRPCClientHandler()` | The client `stats.Handler` itself (for `grpc.WithStatsHandler`) |

```go
// Server
srv := grpc.NewServer(otelhelper.GRPCServerOption())

// Client
conn, err := grpc.NewClient(addr, otelhelper.GRPCDialOption())
```

Every RPC is counted in `rpc.server.call.duration` /
`rpc.client.call.duration`, including server-side `grpc.health.v1.Health/*`;
health server spans are dropped by the helper sampler, and client health calls
get neither a span nor a client metric. The
`*Interceptor()` functions were removed in 0.3.0 (otelgrpc dropped interceptors
in v0.61.0).

### Functional Options

| Option | Description |
|--------|-------------|
| `WithServiceName(name)` | Override service name |
| `WithEnvironment(env)` | Override environment |
| `WithEndpoint(endpoint)` | Generic collector endpoint (URL or `host:port`); `/v1/<signal>` appended for http/protobuf |
| `WithTracesEndpoint` / `WithMetricsEndpoint` / `WithLogsEndpoint` | Per-signal endpoint URL, used verbatim |
| `WithCompression(c)` | `gzip` (default) or `none` |
| `WithDebug()` | Enable debug mode |
| `WithSampleRatio(ratio)` | Set head sampling ratio |
| `WithExportTimeout(ms)` | Export timeout in ms (unset → `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT`, else SDK 10 s) |
| `WithResourceAttributes(attrs)` | Add custom resource attributes |

---

## Behavior per Environment

| Environment | Trace Sampling | Debug Attribute |
|-------------|----------------|-----------------|
| `LOCAL` | 100% (AlwaysOn) | — |
| `DEV` | 100% (AlwaysOn) | — |
| `HML` | 100% (AlwaysOn) | — |
| `PRD` | 100% (AlwaysOn) | — |

> The SDK sends 100% of traces to the Collector in all environments. **Tail-based sampling is the Collector's responsibility** (Agent → Gateway), which decides what to keep based on errors, latency, and configured rate.

When `OTEL_HELPER_DEBUG_LEVEL=true`: entry spans (no parent or a remote parent) get attribute `debug=true`; it is never taken from inbound tracestate/baggage → Collector keeps 100% of these traces via tail sampling policy.

## Behavior when no OTLP endpoint is configured

When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not set**, the library automatically falls back to:

| Signal | Behavior |
|--------|----------|
| Metrics | Exposed via Prometheus HTTP `/metrics` on port 9464 |
| Traces | In-process only (context propagation works, no export) |
| Logs | stdout/console only (no OTel export) |

The Prometheus metrics port is configurable via `OTEL_HELPER_METRICS_PORT` env var (default: 9464).

This enables the standard Kubernetes pattern: deploy without a collector, and let Prometheus/VictoriaMetrics scrape `/metrics` directly from the pod.

---

## What is Configured Automatically

| Signal | What is captured |
|--------|-----------------|
| **Traces** | HTTP requests via `NewHTTPHandler`, HTTP client via `NewHTTPTransport`, gRPC via stats handlers (`GRPCServerOption`/`GRPCDialOption`), custom spans via `GetTracer` |
| **Metrics** | Go runtime metrics (goroutines, GC, memory) automatically, custom meters via `GetMeter`, exported via OTLP with exemplars (SDK default `trace_based`; `OTEL_METRICS_EXEMPLAR_FILTER` honored). Exported every **30s**. |
| **Logs** | Exported via OTLP to the Collector; `NewSlogHandler()`/`NewLogger()` for slog bridge with trace correlation |

## Endpoint Resolution & TLS

Transport is determined by the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA, or `OTEL_EXPORTER_OTLP_CERTIFICATE`) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS; plaintext only with `OTEL_EXPORTER_OTLP_INSECURE=true` |

The scheme always wins: `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` applies only to schemeless endpoints, and a value that contradicts the scheme is warned and ignored. `WithInsecure` likewise applies to schemeless endpoints (it must agree with a code URL's scheme). Certificates, mTLS (`OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`, `_CLIENT_CERTIFICATE`, `_CLIENT_KEY`), headers, timeouts and per-signal endpoints are read by the OTel SDK itself.

For a local plaintext collector, use `http://` in the endpoint or set `OTEL_EXPORTER_OTLP_INSECURE=true`.

## Opt-in Extensions (`ext/`)

AWS, Redis, and SQL instrumentations are available as separate Go modules — not bundled in core:

```bash
go get github.com/staffops/staffops-otel-libs/go/ext/otelaws
go get github.com/staffops/staffops-otel-libs/go/ext/otelredis
go get github.com/staffops/staffops-otel-libs/go/ext/otelsql
```

### Usage

```go
import (
    "github.com/staffops/staffops-otel-libs/go/ext/otelaws"
    "github.com/staffops/staffops-otel-libs/go/ext/otelredis"
    "github.com/staffops/staffops-otel-libs/go/ext/otelsql"
)

// AWS SDK instrumentation
otelaws.Instrument(&cfg)

// Redis client instrumentation
otelredis.Instrument(client)

// SQL with tracing and pool metrics (wraps database/sql)
db, closeDB, err := otelsql.Open(driver, dsn)
if err != nil { ... }
defer closeDB() // instead of db.Close(): also unregisters the pool metrics
```

| Module | Function | What it instruments |
|--------|----------|---------------------|
| `ext/otelaws` | `otelaws.Instrument(&cfg)` | AWS SDK calls |
| `ext/otelredis` | `otelredis.Instrument(client)` (once per client) | go-redis commands (spans with `db.system.name=redis`, no command values, no connection string) and `db.client.connections.*` pool metrics; on a metrics error, tracing is already installed |
| `ext/otelsql` | `db, closeDB, err := otelsql.Open(driver, dsn)` | database/sql queries and `db.sql.*` pool metrics; call `closeDB()` instead of `db.Close()` (it also unregisters the pool metrics); `db.system.name` mapped from the driver (`pgx`/`postgres` → `postgresql`, `sqlite`/`sqlite3` → `sqlite`, `mysql`, `sqlserver`/`mssql` → `microsoft.sql_server`, else `other_sql`) plus legacy `db.system` until 1.0 (only the new key when `OTEL_SEMCONV_STABILITY_OPT_IN` contains `database` without `database/dup`); a pool-metrics registration failure is returned by `Open` |

> **Note**: instrumentation is opt-in by importing the ext module. `WithExtraInstrumentation` / `HasInstrumentation` were removed in 0.3.0; `OTEL_HELPER_EXTRA_INSTRUMENTATION` has no effect in Go and logs a warning when set.

### Health checks: spans dropped, metrics kept

The sampler `Setup` installs drops the **server** span of a health check that
has no local parent (none, or a remote `traceparent`); spans the handler starts
follow it (with the default ParentBased sampler). The requests stay in
`http.server.request.duration` / `rpc.server.call.duration`, so failing probes
remain visible — exclude health routes in SLI queries.

- HTTP: `/health /healthz /ready /readyz /live /livez /ping /metrics` — exact,
  case-insensitive, trailing slash and query ignored (`/users/ping` is traced).
  Replace the list with `WithHealthPaths("/status", ...)`; `WithHealthPaths()`
  traces every path.
- gRPC: the whole `grpc.health.v1.Health/` service — Check, Watch, List (always).

---

## Architecture

```
[ Go App ]
      ↓ OTLP gRPC :4317
[ OTel Collector ]
      ↓
┌──────────┬──────────┬──────────┐
│ Traces   │ Metrics  │ Logs     │
│ (Tempo)  │ (VM)     │ (Loki)   │
└──────────┴──────────┴──────────┘
```

- SDK uses AlwaysOnSampler (default) — sampling is done at the Collector
- SDK only sets `service.name` — resource attributes are enriched by the Collector
- Everything exports via OTLP gRPC to the Collector, never directly to backends

---

## Tests

### Run unit tests

```bash
docker run --rm -v "$(pwd)/go:/src" -w /src golang:1.26 go test ./...
```

### Run examples with Collector

```bash
cd go/example
docker compose up
```

Services:
- `go-api` — HTTP API on `:8080`
- `go-backend` — Backend on `:50051`
- `go-process` — Background worker

---

## Configuration Priority

1. **Code** (functional options in `Setup(ctx, ...)`) — highest priority
2. **Environment variable** — applied if code didn't set it
3. **Library default** — used if neither of the above defined it

---

## Project Structure

```
go/
├── otelhelper.go          # Entry point: Setup(), GetTracer(), GetMeter()
├── options.go             # Options struct + functional options
├── config.go              # Env var resolution + validation
├── tracing.go             # TracerProvider + StartRootSpan
├── metrics.go             # MeterProvider + runtime metrics
├── logging.go             # LoggerProvider + NewSlogHandler, NewLogger, DefaultLogLevel
├── middleware.go          # NewHTTPHandler, NewHTTPTransport + gRPC stats handlers
├── instrumentation.go     # InstrumentSQL placeholder
├── processors.go          # debugProcessor (debug=true attribute)
├── doc.go                 # Package documentation
├── *_test.go              # Unit tests (otelhelper, options, tracing, middleware, extra)
└── example/               # Sample apps
    ├── go-api/            # HTTP API frontend
    ├── go-backend/        # Backend service
    ├── go-process/        # Background worker
    └── protos/            # Reference proto
```
