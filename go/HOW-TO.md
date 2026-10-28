# HOW-TO: Using otelhelper (Go)

Practical guide for Go developers.

---

## 1. HTTP API Setup

```go
package main

import (
    "context"
    "log"
    "net/http"
    "os"
    "os/signal"

    otelhelper "github.com/staffops/staffops-otel-libs/go"
    "go.opentelemetry.io/otel/attribute"
)

func main() {
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
    defer stop()

    shutdown, err := otelhelper.Setup(ctx)
    if err != nil {
        log.Fatalf("otel setup: %v", err)
    }
    defer shutdown(ctx)

    tracer := otelhelper.GetTracer("my-api")
    mux := http.NewServeMux()

    mux.Handle("GET /orders/{id}", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        ctx, span := tracer.Start(r.Context(), "get-order")
        defer span.End()

        id := r.PathValue("id")
        span.SetAttributes(attribute.String("order.id", id))

        // ... business logic ...
        w.Write([]byte(`{"status":"ok"}`))
    }), "GET /orders"))

    // Health check — wrapped so probes count in http.server.request.duration
    // (failing probes stay visible); the helper sampler drops the span.
    mux.Handle("GET /health", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        w.Write([]byte(`{"status":"healthy"}`))
    }), "GET /health"))

    srv := &http.Server{Addr: ":8080", Handler: mux}
    go func() {
        <-ctx.Done()
        srv.Shutdown(context.Background())
    }()

    log.Println("listening on :8080")
    if err := srv.ListenAndServe(); err != http.ErrServerClosed {
        log.Fatal(err)
    }
}
```

Health checks (`/health /healthz /ready /readyz /live /livez /ping /metrics`, exact and case-insensitive) get no span — the sampler `Setup` installs drops it — but still count in `http.server.request.duration`. Replace the list with `otelhelper.WithHealthPaths(...)`.

---

## 1b. HTTP Client Instrumentation

Use `NewHTTPTransport` to instrument outgoing HTTP requests. Client spans are created automatically with context propagation:

```go
import (
    "net/http"
    otelhelper "github.com/staffops/staffops-otel-libs/go"
)

// Create an instrumented HTTP client
client := &http.Client{
    Transport: otelhelper.NewHTTPTransport(nil), // wraps http.DefaultTransport
}

// Requests automatically create client spans and propagate W3C TraceContext
req, _ := http.NewRequestWithContext(ctx, http.MethodPost, "http://backend/process", body)
resp, err := client.Do(req)
```

Requests to a health path (the same list and matching as the server side, including `WithHealthPaths`) get no client span and no client metric.

To wrap a custom transport:

```go
customTransport := &http.Transport{MaxIdleConns: 100}
client := &http.Client{
    Transport: otelhelper.NewHTTPTransport(customTransport),
}
```

---

## 2. gRPC Server Setup

```go
package main

import (
    "context"
    "log"
    "net"
    "os"
    "os/signal"

    otelhelper "github.com/staffops/staffops-otel-libs/go"
    "google.golang.org/grpc"
)

func main() {
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
    defer stop()

    shutdown, err := otelhelper.Setup(ctx)
    if err != nil {
        log.Fatalf("otel setup: %v", err)
    }
    defer shutdown(ctx)

    srv := grpc.NewServer(otelhelper.GRPCServerOption())

    // Register your services
    // pb.RegisterMyServiceServer(srv, &myServer{})

    lis, _ := net.Listen("tcp", ":50051")
    go func() {
        <-ctx.Done()
        srv.GracefulStop()
    }()

    log.Println("gRPC listening on :50051")
    srv.Serve(lis)
}
```

### gRPC Client

```go
conn, err := grpc.NewClient("backend:50051",
    grpc.WithTransportCredentials(insecure.NewCredentials()),
    otelhelper.GRPCDialOption(),
)
defer conn.Close()

client := pb.NewMyServiceClient(conn)
// Calls automatically propagate trace context via gRPC metadata
resp, err := client.MyMethod(ctx, req)
```

gRPC health RPCs (the whole `grpc.health.v1.Health` service: Check, Watch, List) are counted in `rpc.server.call.duration` like any other RPC; their server spans are dropped by the helper sampler (the server stats handler installs no filter, because otelgrpc filters drop metrics too). Client calls to any `grpc.health.v1.Health/*` method (Check, Watch, List) through `GRPCDialOption()` get no client span and no `rpc.client.call.duration`.

---

## 3. Background Worker with StartRootSpan

For workers running in a loop, each iteration should be an independent trace. Use `StartRootSpan`:

```go
package main

import (
    "context"
    "log"
    "os"
    "os/signal"
    "time"

    otelhelper "github.com/staffops/staffops-otel-libs/go"
    "go.opentelemetry.io/otel/attribute"
)

func main() {
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
    defer stop()

    shutdown, err := otelhelper.Setup(ctx)
    if err != nil {
        log.Fatalf("otel setup: %v", err)
    }
    defer shutdown(ctx)

    tracer := otelhelper.GetTracer("my-worker")

    ticker := time.NewTicker(30 * time.Second)
    defer ticker.Stop()

    for {
        select {
        case <-ctx.Done():
            return
        case <-ticker.C:
            // Each iteration is an independent trace
            iterCtx, span := otelhelper.StartRootSpan(ctx, tracer, "process-batch")
            span.SetAttributes(attribute.Int("batch.size", 100))

            processBatch(iterCtx)

            span.End()
        }
    }
}
```

Without `StartRootSpan`, spans from different iterations would share the same trace. With it, each cycle generates an independent trace (new traceId).

---

## 4. Custom Spans and Metrics

### Custom Spans

```go
tracer := otelhelper.GetTracer("my-service")

func processOrder(ctx context.Context, orderID string) error {
    ctx, span := tracer.Start(ctx, "process-order")
    defer span.End()

    span.SetAttributes(attribute.String("order.id", orderID))

    // Child span
    _, dbSpan := tracer.Start(ctx, "query-database")
    // ... db work ...
    dbSpan.End()

    return nil
}
```

### Mark Span with Error

```go
ctx, span := tracer.Start(ctx, "critical-operation")
defer span.End()

if err := doWork(ctx); err != nil {
    span.RecordError(err)
    span.SetStatus(codes.Error, err.Error())
    return err
}
```

### Counter — counts events

```go
meter := otelhelper.GetMeter("my-service")
ordersTotal, _ := meter.Int64Counter("orders.total",
    metric.WithDescription("Total orders processed"))

ordersTotal.Add(ctx, 1, metric.WithAttributes(
    attribute.String("type", "standard"),
))
```

### Histogram — measures distributions (latency, size)

```go
duration, _ := meter.Float64Histogram("request.duration_ms",
    metric.WithDescription("Request duration in milliseconds"))

start := time.Now()
// ... process ...
duration.Record(ctx, float64(time.Since(start).Milliseconds()))
```

### Gauge — value that goes up and down

```go
var pendingItems atomic.Int64

gauge, _ := meter.Int64ObservableGauge("queue.items_pending",
    metric.WithDescription("Items waiting in queue"))

meter.RegisterCallback(func(_ context.Context, o metric.Observer) error {
    o.ObserveInt64(gauge, pendingItems.Load())
    return nil
}, gauge)
```

---

## 5. Distributed Tracing Across Services

Context propagation works automatically via W3C TraceContext headers.

### HTTP → HTTP

```go
// Service A: outgoing request propagates trace context
req, _ := http.NewRequestWithContext(ctx, http.MethodPost, "http://service-b/process", body)
resp, err := http.DefaultClient.Do(req)
```

The `otelhttp` transport (set up by the global propagator) injects `traceparent` headers automatically. Service B extracts them via `NewHTTPHandler`.

### HTTP → gRPC

```go
// Service A: call gRPC backend (the stats handler propagates context)
conn, _ := grpc.NewClient(addr, otelhelper.GRPCDialOption())
client := pb.NewBackendClient(conn)
resp, err := client.Process(ctx, req) // trace context in gRPC metadata
```

### Resulting trace

```
POST /detect (go-api)                    ← NewHTTPHandler
  └── detect.submit (go-api)             ← manual span
       └── POST /detect (go-backend)     ← NewHTTPHandler (child via traceparent)
            └── backend.detect           ← manual span
```

---

## 6. Debug Mode

When `OTEL_HELPER_DEBUG_LEVEL=true`:
- All extra instrumentations are enabled (SQL, REDIS, AWS)
- Root spans get attribute `debug=true` → Collector keeps 100% of these traces
- The `debugProcessor` injects the attribute via span processor

```bash
# Enable for a single run
OTEL_HELPER_DEBUG_LEVEL=true go run .
```

Or via code:

```go
shutdown, _ := otelhelper.Setup(ctx, otelhelper.WithDebug())
```

The Collector uses the `debug-forced-attribute` policy (type: string_attribute, key: debug, values: ["true"]) to ensure debug traces are never dropped by tail sampling.

⚠️ **Do not leave debug enabled in production for long** — generates high volume and increases storage cost.

---

## 7. slog Integration

The lib configures an OTel log provider. Use `log/slog` for structured logging:

```go
import "log/slog"

func processOrder(ctx context.Context, orderID string) {
    slog.InfoContext(ctx, "processing order",
        "order_id", orderID,
        "batch_size", 50,
    )
    // traceId and spanId are correlated automatically when within a span
}
```

### Setting up slog with OTel bridge

The lib provides `NewSlogHandler()` and `NewLogger()` for easy slog integration:

```go
import (
    "log/slog"
    otelhelper "github.com/staffops/staffops-otel-libs/go"
)

// Loggers may be created before or after otelhelper.Setup(): each record goes
// through the OTel bridge while a logs exporter is installed, and to stdout as
// JSON otherwise (no endpoint, OTEL_LOGS_EXPORTER=none, after Shutdown).

// Option 1: Use NewLogger (recommended) — includes level filter per environment
logger := otelhelper.NewLogger(otelhelper.PRD, false) // PRD = Warning level
slog.SetDefault(logger)

// Option 2: Use NewSlogHandler directly (no environment level; Info minimum on stdout)
handler := otelhelper.NewSlogHandler()
logger := slog.New(handler)
slog.SetDefault(logger)

// With a logs exporter, slog calls export via OTLP. Trace correlation
// (trace_id/span_id) needs the *Context calls: InfoContext, WarnContext, …
// — slog.Info(...) carries no context, so the record is not correlated.
slog.InfoContext(ctx, "order completed", "order_id", "123")

// Without a logs exporter each record is one JSON line on stdout:
// {"time":"…","level":"ERROR","msg":"charge failed","order_id":"123","exception":"card declined"}
// (time in UTC with milliseconds; the first error-valued attribute passed with
// the record becomes "exception" — attributes from WithAttrs keep their key;
// a typed-nil error renders "<nil>", a panicking Error() "!PANIC: …")
```

### Log level per environment

`DefaultLogLevel()` returns the appropriate level:

| Environment | Level |
|-------------|-------|
| LOCAL | Debug |
| DEV/HML | Info |
| PRD | Warning |
| unset / any other value | Info |

`debug=true` forces Debug in any environment.

---

## 8. Graceful Shutdown Pattern

The recommended pattern uses `signal.NotifyContext` for clean shutdown:

```go
func main() {
    // 1. Context that cancels on SIGINT/SIGTERM
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer stop()

    // 2. Setup telemetry
    shutdown, err := otelhelper.Setup(ctx)
    if err != nil {
        log.Fatalf("otel setup: %v", err)
    }
    defer func() {
        // ctx is already cancelled when this runs: flush under a fresh deadline.
        flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()
        _ = shutdown(flushCtx) // flushes logs, metrics and traces concurrently
    }()

    // 3. Start server
    srv := &http.Server{Addr: ":8080", Handler: mux}
    go func() {
        <-ctx.Done()
        // Give in-flight requests 5s to complete
        shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()
        srv.Shutdown(shutdownCtx)
    }()

    if err := srv.ListenAndServe(); err != http.ErrServerClosed {
        log.Fatal(err)
    }
}
```

**Order matters:**
1. Signal cancels context → server stops accepting new requests
2. Server drains in-flight requests
3. The deferred `shutdown` flushes pending spans/metrics/logs to the Collector.
   Pass it a fresh context: the signal context is already cancelled, and a
   cancelled context flushes nothing. The helper installs no signal handlers;
   without `syscall.SIGTERM` in `NotifyContext`, `docker stop` / Kubernetes
   kill the process and the deferred call never runs.

A periodic metric export that is in flight when `Shutdown` starts is cancelled
by the SDK reader and reported once on stderr through the OTel error handler
(`failed to upload metrics: context canceled`). It is cosmetic: the final
collection is exported by `Shutdown` itself. The helper does not suppress it,
because the same message from a deadline that was really too short must stay
visible.

---

## 9. Configuration Table

| Variable | Description | Default |
|----------|-------------|---------|
| `OTEL_SERVICE_NAME` | Service name (beats `service.name` in `OTEL_RESOURCE_ATTRIBUTES`, which beats `SERVICE_NAME`) | SDK `unknown_service:<exe>` + warning |
| `SERVICE_NAME` | Deprecated service-name alias | _(unset)_ |
| `ENVIRONMENT` | Deprecated alias for `deployment.environment.name`; `OTEL_RESOURCE_ATTRIBUTES` wins; non-standard values verbatim | _(unset → attribute absent + warning)_ |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint | `http://localhost` |
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode | `false` |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Extra instrumentations | `SQL` |
| `OTEL_HELPER_SAMPLE_RATIO` | Sampling ratio (0.0-1.0). Ignored if the standard `OTEL_TRACES_SAMPLER` is set | `1.0` (AlwaysOn) |
| `OTEL_HELPER_METRICS_PORT` | Standalone Prometheus `/metrics` listener port (`0` disables it) | `9464` |
| `OTEL_METRICS_EXPORTER` | Metric exporter(s): `otlp`, `prometheus`, `otlp,prometheus`, `none` | legacy inference |
| `OTEL_METRIC_EXPORT_INTERVAL` | OTLP metric export interval (ms) | `30000` |
| `OTEL_TRACES_SAMPLER` | Standard OTel sampler config — takes priority over `OTEL_HELPER_SAMPLE_RATIO` when set | _(unset)_ |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | OTLP wire protocol: `grpc` or `http/protobuf` | port-based inference |

### Configuration Priority

1. **Code** (functional options) — highest priority
2. **Standard OTel environment variable** (`OTEL_METRICS_EXPORTER`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_TRACES_SAMPLER`) — applied if code didn't set the equivalent option
3. **`OTEL_HELPER_*` environment variable** — applied if neither of the above is present
4. **Library default** — used if none of the above defined it

---

## 10. Naming Rules and Anti-patterns

### Naming

| Type | Convention | Example |
|------|------------|---------|
| Metrics | snake_case with unit | `request.duration_ms`, `orders.total` |
| Spans | kebab-case or dot.notation | `process-order`, `backend.detect` |
| Attributes | dot.notation | `order.id`, `batch.size` |

### What NOT to do

- ❌ Configure sampling in code — it's the Collector's responsibility
- ❌ Export directly to Tempo/Loki/Prometheus — everything goes through the Collector
- ❌ Create metrics with unlimited labels (`user_id`, `request_id`)
- ❌ Add resource attributes (`service.version`, `k8s.*`) — the Collector does this
- ❌ Forget to call `span.End()` — use `defer span.End()` immediately after creation
- ❌ Pass `context.Background()` to operations — always propagate the request context

---

## 11. FAQ

**Q: Will my app crash if the Collector is down?**
A: No. The SDK exports telemetry asynchronously. If the Collector is unavailable, data is silently discarded after timeout (10s default). The application continues functioning normally.

**Q: Can I call `Setup()` more than once?**
A: Yes. While a pipeline is installed, later calls return its `Shutdown` function (guarded by a `sync.Mutex`). A failed `Setup` installs nothing and releases what it built, so you can retry. After `Shutdown` has run (it is idempotent), the next `Setup` builds a fresh pipeline.

**Q: Do I need to set resource attributes like `k8s.pod.name`?**
A: No. The Collector enriches automatically via `k8sattributes`.

**Q: How to correlate logs with traces?**
A: Use `slog` with the OTel bridge. Logs emitted within a span context include `traceId`/`spanId` automatically.

**Q: Do I need to configure sampling?**
A: No. The SDK uses AlwaysOn by default. Tail-based sampling is done at the Collector. Use `OTEL_HELPER_SAMPLE_RATIO` only in extreme volume scenarios.

**Q: How does context propagation work across services?**
A: The lib sets up W3C TraceContext + Baggage propagators globally. For HTTP, use `http.NewRequestWithContext(ctx, ...)`. For gRPC, use the provided interceptors. Context is propagated automatically.

---

## 12. Opt-in Extensions (`ext/`)

AWS, Redis, and SQL instrumentations are available as **separate Go modules** — not bundled in core. Install only what you need:

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

// AWS SDK instrumentation — pass your aws.Config
otelaws.Instrument(&cfg)

// Redis client instrumentation — pass your go-redis client
otelredis.Instrument(client)

// SQL with tracing — wraps database/sql Open
db, err := otelsql.Open(driver, dsn)
```

| Module | Function | What it instruments |
|--------|----------|---------------------|
| `ext/otelaws` | `otelaws.Instrument(&cfg)` | AWS SDK calls (S3, SQS, DynamoDB, etc.) |
| `ext/otelredis` | `otelredis.Instrument(client)` | go-redis commands |
| `ext/otelsql` | `otelsql.Open(driver, dsn)` | database/sql queries |

> **Note**: The `OTEL_HELPER_EXTRA_INSTRUMENTATION` env var still works for backward compatibility, but explicit ext modules are the recommended approach.

---

## 13. Metrics Without a Collector (Prometheus `/metrics`)

When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not set**, the library automatically falls back to local-only mode:

| Signal | Behavior |
|--------|----------|
| Metrics | Exposed via Prometheus HTTP `/metrics` on port 9464 |
| Traces | In-process only (context propagation works, no export) |
| Logs | stdout/console only (no OTel export) |

The port is configurable via `OTEL_HELPER_METRICS_PORT` env var (default: `9464`).

This enables the standard Kubernetes scrape pattern: deploy without a Collector, and let Prometheus/VictoriaMetrics scrape `/metrics` directly from the pod.

### Running OTLP push AND `/metrics` at the same time

`OTEL_METRICS_EXPORTER` selects which exporter(s) are active on the metrics pipeline, independent of the legacy endpoint-based fallback above:

| `OTEL_METRICS_EXPORTER` | Behavior |
|---|---|
| _(unset)_ | Legacy: OTLP if `OTEL_EXPORTER_OTLP_ENDPOINT` is set, else Prometheus fallback |
| `otlp` | OTLP only. Fails validation at startup if no endpoint is configured |
| `prometheus` | `/metrics` only, even when an OTLP endpoint IS set |
| `otlp,prometheus` | **Both** — one `MeterProvider`, two readers. Same instrument, same values, in both outputs |
| `none` | Metrics disabled entirely (equivalent to `OTEL_HELPER_DISABLED_SIGNALS=metrics`) |

The equivalent functional option — `WithMetricExporters(...)` — wins over the env var:

```go
shutdown, err := otelhelper.Setup(ctx,
    otelhelper.WithMetricExporters("otlp", "prometheus"),
)
```

`OTEL_METRIC_EXPORT_INTERVAL` (milliseconds) controls the OTLP push interval; the library default is 30000ms, overriding the SDK's own 60s default. `WithExportInterval(ms)` wins over the env var.

### Mounting `/metrics` on your own mux

The standalone listener binds its own `net.Listener` on `OTEL_HELPER_METRICS_PORT` — fine for workers/CLIs without their own HTTP server, but not ideal if the app already runs its own mux (or multiple replicas share a pod). Use `MetricsHandler()` and disable the standalone listener:

```go
shutdown, err := otelhelper.Setup(ctx,
    otelhelper.WithMetricExporters("prometheus"), // or "otlp", "prometheus" for dual mode
    otelhelper.WithoutMetricsListener(),           // or WithPrometheusMetricsPort(0)
)

mux := http.NewServeMux()
mux.Handle("/metrics", otelhelper.MetricsHandler())
```

Status: `503` before `Setup`, after a failed `Setup` and after `Shutdown`; `200` with an empty body when Prometheus is deliberately off (`OTEL_SDK_DISABLED=true`, or `OTEL_METRICS_EXPORTER` without `prometheus`), so the target isn't reported down.

`MetricsHandler()` serves off a dedicated `prometheus.Registry` — not the `client_golang` global one — so it stays isolated from any metrics the application registers itself.

### Scoping down what gets scraped

`OTEL_HELPER_DISABLED_METRICS` drops instruments from **every** active exporter — there's no supported way to push everything via OTLP while exposing only a subset on `/metrics` from the same `MeterProvider` (the OTel SDK's Views are per-provider, not per-reader). If you need a narrower `/metrics` output while keeping the full set on OTLP, filter on the scrape side instead — e.g. Prometheus `metric_relabel_configs` or a vmagent `drop` relabel rule matching the instrument name.

---

## 14. Metrics Export Interval

Metrics are exported every **30 seconds** by default (not the SDK's 60s default) — set explicitly via `WithExportInterval(ms)` or `OTEL_METRIC_EXPORT_INTERVAL` (ms). Applies to both OTLP export and the Prometheus `/metrics` fallback.

---

## 15. TLS Transport

OTLP export uses TLS by default. The transport is derived from the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA, or `OTEL_EXPORTER_OTLP_CERTIFICATE`) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS; plaintext only with `OTEL_EXPORTER_OTLP_INSECURE=true` |

Override via env var:

```bash
# TLS endpoint (default behavior for https:// or schemeless)
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-gateway.example:4317

# Force plaintext for a local collector without TLS
OTEL_EXPORTER_OTLP_INSECURE=true
```

Or in code:

```go
shutdown, err := otelhelper.Setup(ctx,
    otelhelper.WithInsecure(true),
)
```

`WithInsecure` / `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` apply to **schemeless** endpoints only. With `http://` or `https://` the scheme decides; a contradicting `INSECURE` is warned and ignored for that signal. Custom CAs and mTLS come from `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE` / `_CLIENT_CERTIFICATE` / `_CLIENT_KEY`, read by the SDK.

---

### SDK log output of raw env values

The helper's own warnings never print header values, TLS file paths or URL
userinfo/query. The OTel Go SDK still parses `OTEL_EXPORTER_OTLP_*` itself and
reports problems through its global logger (`otel.SetLogger`), which can echo
the raw value — for example a malformed `OTEL_EXPORTER_OTLP_HEADERS` item or an
endpoint it cannot parse. The helper does not rewrite environment variables
(G7), so keep secrets in well-formed headers and route the SDK logger
accordingly if your logs leave the pod.

---

## 16. OTLP Protocol (gRPC vs HTTP/protobuf)

By default, the SDK exports over **gRPC**. To use OTLP/HTTP instead (e.g. behind an ingress that doesn't proxy gRPC), set the standard env var:

```bash
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

Or in code:

```go
shutdown, err := otelhelper.Setup(ctx,
    otelhelper.WithOtlpProtocol("http/protobuf"),
)
```

If left unset, the protocol is inferred from the endpoint port: `4318` resolves to `http/protobuf`, any other port (including the default `4317`) resolves to `grpc`.

Priority: code config (`WithOtlpProtocol(...)`) > `OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL` > `OTEL_EXPORTER_OTLP_PROTOCOL` > port-based inference on that signal's endpoint (`4318` → `http/protobuf`) > `grpc` default.

`http/json` is a valid value per the OTel spec but has no Go exporter: in code it fails `Setup`; from the environment it is warned and ignored.

For http/protobuf, `/v1/traces`, `/v1/metrics`, `/v1/logs` are appended to the generic endpoint (env or `WithEndpoint`), keeping any path prefix; per-signal endpoints (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `WithTracesEndpoint`, …) are used verbatim. A URL without a port uses the scheme default (443/80) for http/protobuf, and `:4317` (with a warning) for gRPC.

For local development with a plaintext collector, the default `http://localhost` already resolves to plaintext — no changes needed.