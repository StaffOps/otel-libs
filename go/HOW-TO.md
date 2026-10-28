# HOW-TO: Using otelhelper (Go)

Practical guide for Go developers, for the 0.3.0 contract. Reference tables
(every variable, option and function) are in the [README](README.md); runnable
code is in [example/](example/README.md).

Every snippet assumes the [lifecycle pattern of §8](#8-lifecycle-and-graceful-shutdown):
`signal.NotifyContext` with `SIGTERM`, and `Shutdown` called with a fresh
deadline.

---

## 1. HTTP API setup

```go
package main

import (
    "context"
    "errors"
    "log"
    "net/http"
    "os"
    "os/signal"
    "syscall"
    "time"

    otelhelper "github.com/staffops/otel-libs/go"
    "go.opentelemetry.io/otel/attribute"
)

func main() {
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer stop()

    shutdown, err := otelhelper.Setup(ctx)
    if err != nil {
        log.Fatalf("otel setup: %v", err)
    }
    defer func() {
        flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()
        _ = shutdown(flushCtx)
    }()

    tracer := otelhelper.GetTracer("my-api")
    mux := http.NewServeMux()

    // Server span: "GET /orders/{id}" (from the mux pattern), http.route set.
    mux.Handle("GET /orders/{id}", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        ctx, span := tracer.Start(r.Context(), "get-order")
        defer span.End()
        span.SetAttributes(attribute.String("order.id", r.PathValue("id")))
        _ = ctx // pass ctx to everything the handler calls
        w.Write([]byte(`{"status":"ok"}`))
    }), "get-order"))

    // Wrapped so probes count in http.server.request.duration (failing probes
    // stay visible); the helper sampler drops the span.
    mux.Handle("GET /health", otelhelper.NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        w.Write([]byte(`{"status":"healthy"}`))
    }), "health"))

    srv := &http.Server{Addr: ":8080", Handler: mux}
    go func() {
        <-ctx.Done()
        drainCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()
        _ = srv.Shutdown(drainCtx) // drain before the deferred telemetry flush
    }()
    if err := srv.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
        log.Print(err)
    }
}
```

The `operation` argument of `NewHTTPHandler` is ignored since 0.3.0: span names
come from the route pattern. You can also wrap the whole mux once —
`otelhelper.NewHTTPHandler(mux, "")` — and still get per-route names.

Health checks (`/health /healthz /ready /readyz /live /livez /ping /metrics`,
exact, case-insensitive) get no span but still count in
`http.server.request.duration`. Replace the list with
`otelhelper.WithHealthPaths(...)`.

---

## 1b. HTTP client instrumentation

```go
client := &http.Client{Transport: otelhelper.NewHTTPTransport(nil)} // wraps http.DefaultTransport

req, _ := http.NewRequestWithContext(ctx, http.MethodPost, "http://backend/process", body)
resp, err := client.Do(req) // client span "HTTP POST", traceparent injected
```

Pass the **span's** `ctx` (the one returned by `tracer.Start`), not
`r.Context()`, or the client span becomes a sibling of your span instead of its
child. Requests to a health path (same list and matching as the server side,
`WithHealthPaths` included) get no client span and no client metric.

To wrap a custom transport: `otelhelper.NewHTTPTransport(&http.Transport{MaxIdleConns: 100})`.

---

## 2. gRPC

```go
// Server
srv := grpc.NewServer(otelhelper.GRPCServerOption())
// pb.RegisterMyServiceServer(srv, &myServer{})
go func() { <-ctx.Done(); srv.GracefulStop() }()
_ = srv.Serve(lis)

// Client
conn, err := grpc.NewClient("backend:50051",
    grpc.WithTransportCredentials(insecure.NewCredentials()),
    otelhelper.GRPCDialOption(),
)
defer conn.Close()
resp, err := pb.NewMyServiceClient(conn).MyMethod(ctx, req) // context in gRPC metadata
```

The stats handlers cover unary and streaming RPCs. Metrics:
`rpc.server.call.duration` / `rpc.client.call.duration` in seconds, with
`rpc.method="pkg.Svc/Method"` (`OTEL_SEMCONV_STABILITY_OPT_IN=rpc/dup` emits the
old `rpc.*.duration` set too).

gRPC health RPCs (the whole `grpc.health.v1.Health` service: Check, Watch,
List) are counted in `rpc.server.call.duration`; their server spans are dropped
by the helper sampler. Client calls to `grpc.health.v1.Health/*` through
`GRPCDialOption()` get no client span and no `rpc.client.call.duration`.
Long-lived `Watch` streams inflate tail latency: exclude
`rpc_method=~"grpc.health.v1.Health/.*"` in SLO queries.

---

## 3. Background worker with `StartRootSpan`

For loops, consumers and cron jobs, each unit of work should be its own trace:

```go
tracer := otelhelper.GetTracer("my-worker")
ticker := time.NewTicker(30 * time.Second)
defer ticker.Stop()

for {
    select {
    case <-ctx.Done():
        return
    case <-ticker.C:
        iterCtx, span := otelhelper.StartRootSpan(ctx, tracer, "process-batch")
        span.SetAttributes(attribute.Int("batch.size", 100))
        processBatch(iterCtx)
        span.End()
    }
}
```

Without `StartRootSpan`, spans started from a context that already carries a
span would join that trace; `StartRootSpan` always starts a new trace ID. On
shutdown, let the in-flight iteration finish (wait for the worker goroutine)
before calling `Shutdown`, so its span is exported.

---

## 4. Custom signals

The runnable version of this section is
[`example/go-custom-signals`](https://github.com/StaffOps/otel-libs/blob/main/go/example/go-custom-signals/main.go);
its output is in the [example README](example/README.md#go-custom-signals). It
uses the same names and trace shape as the Python example, so the two can be
compared side by side.

### Spans: child spans, attributes, events, errors

```go
var tracer = otelhelper.GetTracer("orders")

// PaymentDeclinedError is returned unwrapped where it is recorded, so the
// span's exception.type names it (*main.PaymentDeclinedError); a wrapped error
// (fmt.Errorf("...: %w", err)) would be recorded as *fmt.wrapError.
type PaymentDeclinedError struct{ OrderID string }

func (e *PaymentDeclinedError) Error() string { return "card declined by issuer" }

func processOrder(ctx context.Context, o Order) error {
    ctx, span := tracer.Start(ctx, "order.process", // child of the span in ctx
        trace.WithAttributes(
            attribute.String("order.id", o.ID),
            attribute.String("order.type", o.Type),
        ))
    defer span.End()

    // Event: a timestamped annotation inside the span.
    span.AddEvent("order.validated", trace.WithAttributes(attribute.Float64("order.amount", o.Amount)))

    if err := charge(ctx, o); err != nil {
        span.RecordError(err)                          // "exception" event (type + message)
        span.SetStatus(codes.Error, "payment declined") // marks the span failed
        return err
    }
    return nil
}
```

The example wraps the per-order spans in a batch root span,
`otelhelper.StartRootSpan(ctx, tracer, "orders.process-batch", …)`: a span that
must start a new trace ([§3](#3-background-worker-with-startrootspan)).

### Metrics: counter, histogram, up-down counter, observable gauge

```go
meter := otelhelper.GetMeter("orders")

processed, _ := meter.Int64Counter("order.processed",
    metric.WithDescription("Orders processed"), metric.WithUnit("{order}"))
duration, _ := meter.Float64Histogram("order.duration",
    metric.WithUnit("s"),
    metric.WithExplicitBucketBoundaries(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1))
active, _ := meter.Int64UpDownCounter("order.active", metric.WithUnit("{order}"))
depth, _ := meter.Int64ObservableGauge("order.queue.depth", metric.WithUnit("{order}"))

// Observable instruments are read by a callback at each collection.
_, _ = meter.RegisterCallback(func(_ context.Context, o metric.Observer) error {
    o.ObserveInt64(depth, int64(queue.Len()), metric.WithAttributes(attribute.String("queue.name", "orders")))
    return nil
}, depth)

attrs := []attribute.KeyValue{attribute.String("order.type", "express")}
if err != nil {
    attrs = append(attrs, attribute.String("error.type", "PaymentDeclinedError")) // failures only
}
active.Add(ctx, 1, metric.WithAttributes(attrs[0]))
defer active.Add(ctx, -1, metric.WithAttributes(attrs[0]))
processed.Add(ctx, 1, metric.WithAttributes(attrs...))
duration.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(attrs...))
```

Recording with the span's `ctx` attaches **exemplars** (trace and span ID) to
the data points. Attributes on metrics must be low-cardinality: `order.type`,
`error.type` — never `order.id` or a user ID (put those on spans). The SDK's
default histogram buckets (`0, 5, 10, … 10000`) suit milliseconds; give
second-based histograms explicit boundaries.

### Logs correlated with the trace

```go
logger := slog.New(otelhelper.NewSlogHandler()) // or otelhelper.NewLogger(env, debug)

logger.InfoContext(ctx, "order processed", "order.id", o.ID, "order.amount", o.Amount)
logger.ErrorContext(ctx, "order failed", "order.id", o.ID, "err", err)
```

With a logs exporter installed, each record is exported with the `trace_id`
and `span_id` of the span in `ctx`. An attribute whose value is an `error`
(any key; the key itself is not exported) becomes `exception.message` /
`exception.type` on the exported record. `logger.Info` (without `Context`)
exports a record with no trace correlation. Without a logs exporter the record
is a stdout JSON line (no trace IDs) — see [§7](#7-slog-integration).

---

## 5. Distributed tracing across services

Propagation (W3C TraceContext + Baggage) is automatic through
`NewHTTPTransport` / `NewHTTPHandler` and the gRPC stats handlers. A plain
`http.DefaultClient` does **not** inject `traceparent`: use
`NewHTTPTransport`.

Resulting trace for `POST /detect` in the [example apps](example/README.md)
(recorded from the Collector, otelhttp v0.72):

```
POST /detect                (go-api, Server)     ← NewHTTPHandler
└── detect.submit           (go-api, Internal)   ← manual span
    └── HTTP POST           (go-api, Client)     ← NewHTTPTransport
        └── POST /detect    (go-backend, Server) ← NewHTTPHandler, parent via traceparent
            └── backend.detect (go-backend, Internal)
```

---

## 6. Debug mode

`OTEL_HELPER_DEBUG_LEVEL=true` (or `WithDebug()`) turns on, together
([ADR-006](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-006-debug-span-attribute.md)):

- DEBUG log level for the helper's slog handler;
- attribute `debug=true` on every **entry span** — no parent, or a remote
  parent — so the Collector's tail-sampling policy keeps the trace. Spans with
  a local parent are not marked; inbound tracestate/baggage never sets it, and
  tracestate is not written.

```bash
OTEL_HELPER_DEBUG_LEVEL=true go run .
```

Collector side: a `string_attribute` policy on `debug=true`, combined (`and`)
with an environment allow-list and a `rate_limiting` sub-policy (design D11).

Limit: under the default ParentBased sampler an entry span whose remote parent
is **unsampled** is dropped before the marker runs — debug mode cannot force
retention of traces an upstream caller did not sample.

> Do not leave debug on in production: it raises volume and storage cost.

---

## 7. slog integration

```go
logger := slog.New(otelhelper.NewSlogHandler()) // level: the one Setup resolved (INFO before Setup)
// or: logger := otelhelper.NewLogger(otelhelper.PRD, false) // fixed level for that environment
slog.SetDefault(logger)

slog.InfoContext(ctx, "order completed", "order_id", "123") // correlated
slog.Info("order completed", "order_id", "123")             // NOT correlated (no ctx)
```

Loggers may be created before or after `Setup`. The handler decides per record:

| State | Where records go |
|-------|-----------------|
| A logs exporter is installed (`otlp` and/or `OTEL_LOGS_EXPORTER=console`; `otlp,console` feeds both) | OTel bridge → OTLP / console, with `trace_id`/`span_id` from `ctx` |
| Before `Setup`, no logs endpoint, `OTEL_LOGS_EXPORTER=none`, after `Shutdown` | stdout, one JSON line |

Stdout line (shared with Python):

```json
{"time":"2026-10-28T17:21:06.158Z","level":"ERROR","msg":"order failed","order.id":"ord-3","exception":"charge ord-3: card declined"}
```

- `time`: RFC 3339, UTC, milliseconds. `level`: `DEBUG`/`INFO`/`WARN`/`ERROR`.
- The first error-valued attribute passed with the record is renamed
  `exception` (its `Error()` text) — not when an `exception` key exists, and not
  for attributes added with `WithAttrs`. A typed-nil error renders `<nil>`, a
  panicking `Error()` renders `!PANIC: …`.

Exported records use the instrumentation scope `otel-helper`.

### Log level per environment

| `deployment.environment.name` | Level |
|-------------|-------|
| `LOCAL` | Debug |
| `DEV` / `HML` | Info |
| `PRD` | Warn |
| unset / any other value | Info |

`OTEL_HELPER_DEBUG_LEVEL=true` forces Debug. `NewSlogHandler` uses the level
the last `Setup` resolved and `NewLogger(env, debug)` uses `env`'s level; both
apply it on the stdout path and on the OTel bridge (same as Python).

Helper **diagnostics** (warnings about configuration) are separate: they go to
stderr through the helper's own text logger, whatever the app does with
`slog.Default()`.

---

## 8. Lifecycle and graceful shutdown

```go
func run() error {
    // 1. Cancel on SIGINT and SIGTERM (docker stop / Kubernetes send SIGTERM).
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer stop()

    // 2. Telemetry.
    shutdown, err := otelhelper.Setup(ctx)
    if err != nil {
        return fmt.Errorf("otel setup: %w", err)
    }
    defer func() {
        // 4. ctx is cancelled here: flush under a fresh deadline.
        flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()
        if err := shutdown(flushCtx); err != nil {
            log.Printf("otel shutdown: %v", err)
        }
    }()

    // 3. Serve until the signal, then drain (bounded) before the flush.
    srv := &http.Server{Addr: ":8080", Handler: mux}
    srvErr := make(chan error, 1)
    go func() { srvErr <- srv.ListenAndServe() }()
    select {
    case <-ctx.Done():
    case err := <-srvErr:
        return err // still runs the deferred flush
    }
    drainCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
    defer cancel()
    return srv.Shutdown(drainCtx)
}

func main() {
    if err := run(); err != nil { // not log.Fatal inside run: it skips deferred calls
        log.Print(err)
        os.Exit(1)
    }
}
```

Rules ([design D13](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d13--lifecycle-setup-after-shutdown-a6)):

- The helper installs **no signal handlers**. Without `syscall.SIGTERM` in
  `NotifyContext` the default action kills the process and the deferred
  `Shutdown` never runs: buffered spans, logs and the last metric interval are lost.
- Pass `Shutdown` a **fresh** context: the signal context is already cancelled,
  and a cancelled context flushes nothing.
- `log.Fatal` / `os.Exit` skip deferred calls — return errors from `run`.
- Drain servers and workers first, then flush, so their spans are exported.
- `Shutdown` flushes logs, metrics and traces concurrently, is idempotent (later
  or concurrent calls return the first result) and resets the helper.
- `Setup` failure releases everything it built (including the `/metrics` port)
  and installs nothing — fix the configuration and call it again.
- `Setup` after `Shutdown` builds a fresh pipeline; tracers, meters and
  instrumented handlers created **before** it stay bound to the old,
  shut-down providers. Obtain them after `Setup`.

A periodic metric export in flight when `Shutdown` starts is cancelled and
reported once on stderr through the OTel error handler
(`failed to upload metrics: context canceled`). It is cosmetic: `Shutdown`
exports the final collection itself.

---

## 9. Configuration reference

The list of variables, defaults and deprecations is in the
[README](README.md#environment-variables). This section is the resolution
contract ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d1--resolution-contract-the-table-every-language-implements)
and its [edge-case rulings](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d1-edge-case-rulings-a3-validation-2026-10-28)).

### Precedence

1. **Code** (`With*` options).
2. **Standard OTel variable** — per-signal (`OTEL_EXPORTER_OTLP_TRACES_*`)
   before generic (`OTEL_EXPORTER_OTLP_*`).
3. **Deprecated / `OTEL_HELPER_*` variable** (`SERVICE_NAME`, `ENVIRONMENT`,
   `OTEL_HELPER_SAMPLE_RATIO`, `OTEL_HELPER_DISABLED_SIGNALS`).
4. **Default.**

Invalid **env** values warn (stderr, naming the variable, never printing
secrets) and fall back to the next level; invalid **code** values make `Setup`
return an error naming the option.

### OTLP rules

| Setting | Rule |
|---------|------|
| "OTLP configured" | Per signal: a code endpoint, the generic env endpoint, or that signal's env endpoint. Unconfigured signals fall back (traces: not exported; metrics: `/metrics`; logs: stdout) |
| Generic endpoint | `/v1/traces`, `/v1/metrics`, `/v1/logs` appended for http/protobuf, keeping any path prefix (`http://c:4318/otlp` → `/otlp/v1/traces`) |
| Per-signal endpoint | Used verbatim (no path appended) |
| Schemeless `host:port` | Becomes `https://` (`http://` only with `INSECURE=true`), default port 4317 (gRPC) / 4318 (HTTP). For http/protobuf it is deprecated (warned once) |
| URL without port | http/protobuf: scheme default 443 / 80. gRPC: `:4317` + warning "set an explicit port" ([D1.b](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#decision-d1b-portless-grpc-urls-keep-4317-reversed)) |
| Unsupported scheme (`unix://`, `ftp://`, `unix:/path`) | Warned; the signal is **not exported** (also when a valid generic endpoint exists) |
| Unparseable / empty-host per-signal endpoint | Warned; falls back to the generic endpoint |
| Whitespace, userinfo, query | Surrounding whitespace trimmed; userinfo and query stripped with a redacted warning — put credentials in `*_HEADERS` |
| Protocol | Code > `_<SIGNAL>_PROTOCOL` > `OTEL_EXPORTER_OTLP_PROTOCOL` > port `4318` on that signal's endpoint → `http/protobuf` > `grpc` |
| TLS (`*_INSECURE`) | The scheme decides: `https://` TLS, `http://` plaintext. `INSECURE` applies to schemeless endpoints only; a contradicting value is warned and ignored for that signal ([D1.c](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#decision-d1c-tls-conflicts-are-normalized--scheme-wins-revised)). Booleans: `true`/`false` case-insensitive; anything else warns and reads as false |
| Certificates | `*_CERTIFICATE`, `*_CLIENT_CERTIFICATE`, `*_CLIENT_KEY` read by the SDK; with a plaintext endpoint they are warned and ignored for that signal |
| `*_TIMEOUT` | Plain decimal **milliseconds** (`^[0-9]+(\.[0-9]+)?$`); garbage, `0`, negative, hex or exponent warn and fall back (per-signal → generic → SDK 10 000 ms); valid values are clamped to [1 ms, 24 h] with a warning. The value also bounds the batch processors and the metric reader |
| `*_COMPRESSION` | `gzip` or `none`, case-insensitive; anything else (`deflate`, `zstd`) warns and uses gzip. Default `gzip` for every signal ([D1.d](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#decision-d1d-helper-default-compression-gzip)) |
| `*_HEADERS` | `key=value` pairs, comma-separated. Keys must match `[A-Za-z0-9._-]+`, compared case-insensitively (last wins), not decoded. Values percent-decoded; an invalid escape or non-UTF-8 result keeps the whole value literal (warned); a decoded control character skips the item. Malformed items are skipped and warned **by position only**. Applied to every signal |
| Fallback | Every per-signal value (endpoint, timeout, compression, headers): invalid → warn → generic → default |

### Other settings

| Setting | Rule |
|---------|------|
| `service.name` | Code > `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` > `unknown_service:<exe>` + warning |
| `deployment.environment.name` | Code > `OTEL_RESOURCE_ATTRIBUTES` > `ENVIRONMENT` > absent + warning ([ADR-003](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-003-environment-unset-not-local.md)) |
| Sampler | Code ratio (`WithSampleRatio`, any value incl. 1.0) > `OTEL_TRACES_SAMPLER`/`_ARG` > `OTEL_HELPER_SAMPLE_RATIO` (only while `OTEL_TRACES_SAMPLER` is unset) > `parentbased_always_on`; health-drop wrapper on top |
| Signals on/off | `OTEL_SDK_DISABLED=true` (no-op everything) / `OTEL_<SIGNAL>_EXPORTER=none` > `OTEL_HELPER_DISABLED_SIGNALS` |
| Metric exporters | Code > `OTEL_METRICS_EXPORTER` > inference (`otlp` if a metrics endpoint is configured, else `prometheus`). Env `otlp` without an endpoint warns and is dropped; code `otlp` without one fails `Setup` |
| Export interval | Code > `OTEL_METRIC_EXPORT_INTERVAL` (positive integer ms) > 30 000 |
| Propagators | `OTEL_PROPAGATORS` (trimmed, lowercased): `tracecontext`, `baggage`, `none`; others warned and skipped; nothing supported left → default `tracecontext,baggage` |

The helper never calls `os.Setenv`.

---

## 10. Naming rules and anti-patterns

| Type | Convention | Example |
|------|------------|---------|
| Metrics | dot-separated, unit in `WithUnit` (not in the name) | `orders.processing.duration` + `s`, `orders.processed` + `{order}` |
| Spans | low-cardinality operation name | `order.process`, `backend.detect` |
| Attributes | dot-separated, semconv keys where they exist | `order.id`, `http.route` |

- Don't configure sampling in code — tail sampling is the Collector's job.
- Don't export directly to Tempo/Loki/Prometheus — go through the Collector.
- Don't put unbounded values on metrics (`user_id`, `request_id`, `order.id`).
- Don't set `service.version`, `k8s.*` or cloud attributes — the Collector does.
- Always `defer span.End()` right after `Start`.
- Pass the request/span context down; `context.Background()` breaks the trace.
- Use `*Context` slog calls for correlated logs.

---

## 11. FAQ

**Will my app crash if the Collector is down?**
No. Exports are asynchronous; failed batches are retried and then dropped, and
the SDK reports the failure on stderr. The app keeps running.

**Can I call `Setup()` more than once?**
Yes. While a pipeline is installed, later calls return its `Shutdown`. A failed
`Setup` installs nothing, so you can retry. After `Shutdown`, the next `Setup`
builds a fresh pipeline (see the caveat in [§8](#8-lifecycle-and-graceful-shutdown)).

**Do I need to set `k8s.pod.name` or `service.version`?**
No. The Collector adds them (`k8sattributes`).

**How do I correlate logs with traces?**
Log with the `*Context` slog calls and the span's `ctx` ([§4](#logs-correlated-with-the-trace)).

**Do I need to configure sampling?**
No. The SDK uses `parentbased_always_on`; tail sampling happens at the
Collector. For extreme volume use `OTEL_TRACES_SAMPLER=parentbased_traceidratio`
with `OTEL_TRACES_SAMPLER_ARG`.

**How does propagation work?**
W3C TraceContext + Baggage, installed globally. HTTP: `NewHTTPTransport` on the
client and `NewHTTPHandler` on the server, with `http.NewRequestWithContext`.
gRPC: `GRPCDialOption` / `GRPCServerOption`.

---

## 12. Opt-in extensions (`ext/`)

AWS (`ext/otelaws`), Redis (`ext/otelredis`) and SQL (`ext/otelsql`) are
separate modules; usage, attributes and metrics are in the
[README](README.md#opt-in-extensions-ext). The two API points to remember:

```go
db, closeDB, err := otelsql.Open("pgx", dsn)
if err != nil { return err }
defer closeDB() // not db.Close(): also unregisters the pool-metrics callback

if err := otelredis.Instrument(rdb); err != nil { // once per client
    return err // tracing is already installed when the metrics step fails
}
```

`OTEL_HELPER_EXTRA_INSTRUMENTATION` has no effect in Go (warned once).

---

## 13. Metrics without a Collector (Prometheus `/metrics`)

With no metrics endpoint, metrics are served on `:9464/metrics`
(`OTEL_HELPER_METRICS_PORT`) for Prometheus/VictoriaMetrics to scrape.

### Selecting exporters

| `OTEL_METRICS_EXPORTER` | Behavior |
|---|---|
| _(unset)_ | `otlp` if a metrics endpoint is configured, else `prometheus` |
| `otlp` | OTLP only; without an endpoint: warning, falls back to inference |
| `prometheus` | `/metrics` only, even with an OTLP endpoint |
| `otlp,prometheus` | Both — one `MeterProvider`, two readers, same values |
| `console` | Periodic JSON on stdout (debugging) |
| `none` | Metrics disabled |

`WithMetricExporters(...)` wins over the variable (and fails `Setup` for `otlp`
without an endpoint).

### Mounting `/metrics` on your own mux

```go
shutdown, err := otelhelper.Setup(ctx,
    otelhelper.WithMetricExporters("prometheus"), // or "otlp", "prometheus"
    otelhelper.WithoutMetricsListener(),           // or WithPrometheusMetricsPort(0)
)
mux.Handle("/metrics", otelhelper.MetricsHandler())
```

| State | `MetricsHandler()` status |
|-------|--------|
| Before `Setup`, after a failed `Setup`, after `Shutdown` | `503` (transient, scrape retries) |
| Prometheus deliberately off (`OTEL_SDK_DISABLED=true`, or `OTEL_METRICS_EXPORTER` without `prometheus`) | `200`, empty body — the target is not reported down |
| Prometheus reader active | `200` with metrics |

The handler serves a dedicated `prometheus.Registry`, isolated from metrics the
application registers in `client_golang`'s global one. `/metrics` is in the
default health path list, so scrapes of a wrapped mux are not traced.

### Scoping what is exported

`OTEL_HELPER_DISABLED_METRICS` / `WithDisabledMetrics` drop instruments from
**every** exporter (SDK views are per provider). To expose less on `/metrics`
than on OTLP, filter at the scraper (`metric_relabel_configs`, vmagent relabel).

---

## 14. Metrics export interval

Every 30 s by default (the SDK's is 60 s): `WithExportInterval(ms)` >
`OTEL_METRIC_EXPORT_INTERVAL` (ms) > 30 000. It applies to the periodic readers
(OTLP, console); `/metrics` is collected at scrape time.

---

## 15. TLS transport

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA, or `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`) |
| `http://host:4317` | Plaintext |
| `host:4317` (schemeless) | TLS; plaintext only with `OTEL_EXPORTER_OTLP_INSECURE=true` / `WithInsecure(true)` |

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-gateway.example.com:4317
OTEL_EXPORTER_OTLP_CERTIFICATE=/etc/ssl/collector-ca.pem
```

`INSECURE` never overrides a scheme. mTLS: `_CLIENT_CERTIFICATE` +
`_CLIENT_KEY`. A TLS file with a plaintext endpoint is warned and ignored.

### SDK log output of raw env values

The helper's own warnings never print header values, TLS file paths or URL
userinfo/query. The OTel Go SDK, however, still parses some `OTEL_EXPORTER_OTLP_*`
values itself and reports problems through its global logger
(`otel.SetLogger` / `otel.Handle`), which can echo the raw value — for example a
malformed `OTEL_EXPORTER_OTLP_HEADERS` item. The helper does not rewrite
environment variables, so keep header values well-formed and route the SDK
logger accordingly if your logs leave the pod.

---

## 16. OTLP protocol (gRPC vs HTTP/protobuf)

```bash
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf   # or OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL
```

```go
otelhelper.Setup(ctx, otelhelper.WithOtlpProtocol(otelhelper.ProtocolHTTP))
```

Unset: port `4318` on the signal's endpoint selects `http/protobuf`, anything
else `grpc`. `http/json` has no Go exporter: in code it fails `Setup`; from the
environment it is warned and ignored. Path and port rules are in
[§9](#otlp-rules).

There is no default endpoint: without one a signal is not exported over OTLP.
For a local plaintext Collector use `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`.

---

## 17. Troubleshooting

| Symptom | Likely cause / fix |
|---------|-------------------|
| Nothing reaches the Collector, metrics on `:9464` | No endpoint for those signals: set `OTEL_EXPORTER_OTLP_ENDPOINT` (configured is per signal) |
| `service.name` is `unknown_service:<exe>` | `OTEL_SERVICE_NAME` not set (stderr shows a warning) |
| Spans lack `deployment.environment.name`, log level is INFO locally | Set `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=LOCAL` |
| `x509: certificate signed by unknown authority` | Set `OTEL_EXPORTER_OTLP_CERTIFICATE` to the Collector's CA |
| TLS handshake errors against a plaintext Collector | Endpoint is `https://` or schemeless: use `http://` (`INSECURE=true` does not override `https://`) |
| HTTP 404 from the Collector | Per-signal endpoint missing `/v1/<signal>` (used verbatim), or a generic endpoint with `/v1/...` added by hand |
| gRPC export to `https://host` hangs/fails | Portless gRPC URL defaulted to `:4317` (warned): set the real port |
| Logs not correlated with traces | Use `InfoContext(ctx, …)` with the span's `ctx` |
| Logs on stdout instead of OTLP | No logs endpoint, `OTEL_LOGS_EXPORTER=none`, or logging before `Setup` / after `Shutdown` |
| Last spans/logs missing after `docker stop` | No `SIGTERM` in `NotifyContext`, `log.Fatal`, or `Shutdown` called with a cancelled context ([§8](#8-lifecycle-and-graceful-shutdown)) |
| Health probes missing from traces | Expected: dropped by the sampler; they are in the metrics |
| Client span is a sibling of my span | The request used `r.Context()` instead of the span's `ctx` |
| `failed to upload metrics: context canceled` at shutdown | Cosmetic ([§8](#8-lifecycle-and-graceful-shutdown)) |
| A warning names a deprecated variable | Replace it as the message says (README "Environment variables") |
| `undefined: otelhelper.UnaryServerInterceptor` | Use `GRPCServerOption()` / `GRPCDialOption()` |
| `assignment mismatch: 2 variables but otelsql.Open returns 3 values` | `db, closeDB, err := otelsql.Open(...)` |

Inspecting what is sent: `OTEL_TRACES_EXPORTER=otlp,console` (and the same for
logs/metrics) prints every item on stdout as well.

---

## 18. Migrating from 0.2.0

See the [migration table in the README](README.md#migration-from-020) and the
[CHANGELOG](../CHANGELOG.md).
