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
go get github.com/staffops/otel-helper-go
```

Requires Go 1.22+.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_NAME` | `my-service` | Service name (priority over `OTEL_SERVICE_NAME`) |
| `ENVIRONMENT` | `LOCAL` | Environment: LOCAL, DEV, HML, PRD |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost` | Collector endpoint |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode: all instrumentations, attribute debug=true |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Conditional instrumentations: SQL, REDIS, AWS |
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

Wraps an `http.Handler` with OTel tracing. Automatically filters health paths (`/ping`, `/health`, `/healthz`, `/ready`).

```go
mux.Handle("POST /orders", otelhelper.NewHTTPHandler(handler, "POST /orders"))
```

### `NewHTTPTransport(base http.RoundTripper) http.RoundTripper`

Wraps an `http.RoundTripper` with OTel tracing for **outgoing** HTTP requests. Health paths are filtered. Pass `nil` to wrap `http.DefaultTransport`.

```go
client := &http.Client{Transport: otelhelper.NewHTTPTransport(nil)}
resp, err := client.Do(req) // automatically creates client spans with context propagation
```

### `NewSlogHandler() slog.Handler`

Returns an `slog.Handler` that bridges to OTel logs via `otelslog`. Logs emitted within a span context automatically include `trace_id` and `span_id`.

```go
handler := otelhelper.NewSlogHandler()
logger := slog.New(handler)
```

### `DefaultLogLevel(env DeploymentEnvironment, debug bool) slog.Level`

Returns the appropriate `slog.Level` for a given environment: LOCAL=Debug, DEV/HML=Info, PRD=Warning. Debug override forces Debug.

### `NewLogger(env DeploymentEnvironment, debug bool) *slog.Logger`

Returns a configured `*slog.Logger` with OTel bridge and environment-appropriate level filter.

```go
logger := otelhelper.NewLogger(otelhelper.PRD, false) // Warning level, OTel bridge
slog.SetDefault(logger)
```

### gRPC Interceptors

| Function | Description |
|----------|-------------|
| `UnaryServerInterceptor()` | Server-side unary interceptor (filters gRPC health checks) |
| `StreamServerInterceptor()` | Server-side stream interceptor (filters gRPC health checks) |
| `UnaryClientInterceptor()` | Client-side unary interceptor (filters gRPC health checks) |
| `StreamClientInterceptor()` | Client-side stream interceptor (filters gRPC health checks) |

```go
// Server
srv := grpc.NewServer(
    grpc.UnaryInterceptor(otelhelper.UnaryServerInterceptor()),
    grpc.StreamInterceptor(otelhelper.StreamServerInterceptor()),
)

// Client
conn, _ := grpc.Dial(addr,
    grpc.WithUnaryInterceptor(otelhelper.UnaryClientInterceptor()),
    grpc.WithStreamInterceptor(otelhelper.StreamClientInterceptor()),
)
```

### Functional Options

| Option | Description |
|--------|-------------|
| `WithServiceName(name)` | Override service name |
| `WithEnvironment(env)` | Override environment |
| `WithEndpoint(endpoint)` | Override collector endpoint |
| `WithDebug()` | Enable debug mode |
| `WithSampleRatio(ratio)` | Set head sampling ratio |
| `WithExportTimeout(ms)` | Set export timeout in ms |
| `WithExtraInstrumentation(instr)` | Set extra instrumentations |
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

When `OTEL_HELPER_DEBUG_LEVEL=true`: root spans get attribute `debug=true` → Collector keeps 100% of these traces via tail sampling policy.

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
| **Traces** | HTTP requests via `NewHTTPHandler`, HTTP client via `NewHTTPTransport`, gRPC via interceptors, custom spans via `GetTracer` |
| **Metrics** | Go runtime metrics (goroutines, GC, memory) automatically, custom meters via `GetMeter`, exported via OTLP with exemplars (`OTEL_METRICS_EXEMPLAR_FILTER=trace_based` auto-set). Exported every **30s**. |
| **Logs** | Exported via OTLP to the Collector; `NewSlogHandler()`/`NewLogger()` for slog bridge with trace correlation |

## Endpoint Resolution

Endpoints without scheme (e.g. `collector.svc:4317`) are automatically prefixed with `http://`. No more `scheme://None` errors.

## Opt-in Extensions (`ext/`)

AWS, Redis, and SQL instrumentations are available as separate Go modules — not bundled in core:

```bash
go get github.com/StaffOps/staffops-otel-libs/go/ext/otelaws
go get github.com/StaffOps/staffops-otel-libs/go/ext/otelredis
go get github.com/StaffOps/staffops-otel-libs/go/ext/otelsql
```

### Usage

```go
import (
    "github.com/StaffOps/staffops-otel-libs/go/ext/otelaws"
    "github.com/StaffOps/staffops-otel-libs/go/ext/otelredis"
    "github.com/StaffOps/staffops-otel-libs/go/ext/otelsql"
)

// AWS SDK instrumentation
otelaws.Instrument(&cfg)

// Redis client instrumentation
otelredis.Instrument(client)

// SQL with tracing (wraps database/sql)
db, err := otelsql.Open(driver, dsn)
```

| Module | Function | What it instruments |
|--------|----------|---------------------|
| `ext/otelaws` | `otelaws.Instrument(&cfg)` | AWS SDK calls |
| `ext/otelredis` | `otelredis.Instrument(client)` | go-redis commands |
| `ext/otelsql` | `otelsql.Open(driver, dsn)` | database/sql queries |

> **Note**: `OTEL_HELPER_EXTRA_INSTRUMENTATION` env var still works for backward compatibility but explicit extensions are recommended.

### Health Checks Filtered

The lib **does not generate spans** for:
- `/ping`, `/health`, `/healthz`, `/ready` (HTTP)
- `/grpc.health.v1.Health/Check` (gRPC)

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
docker run --rm -v "$(pwd)/go:/src" -w /src golang:1.22 go test ./...
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
├── middleware.go          # NewHTTPHandler, NewHTTPTransport + gRPC interceptors
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
