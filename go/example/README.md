# Go examples

Four programs using the `otelhelper` library:

| Example | What it shows |
|---------|---------------|
| [`go-custom-signals`](#go-custom-signals) | Creating your own telemetry: a batch root span with one child span per order, attributes, events, a named error type and error status; counter, histogram, up-down counter, observable gauge; logs correlated with the trace. Runs one batch and exits. Same names and trace shape as the Python twin |
| `go-api` | HTTP API: `NewHTTPHandler`, `NewHTTPTransport` to the backend, custom spans and metrics |
| `go-backend` | HTTP service called by `go-api` (trace context via `traceparent`) |
| `go-process` | Background workers: one trace per iteration with `StartRootSpan`, observable gauge, correlated logs |

All of them use the lifecycle pattern from [HOW-TO §8](../HOW-TO.md#8-lifecycle-and-graceful-shutdown):
`SIGTERM` handled with `signal.NotifyContext`, servers and workers drained,
then `Shutdown` flushes with a fresh deadline — so `docker stop` loses nothing.

## Build

The build context is `go/`: each example's `go.mod` replaces the helper with
`../../`, so the helper source must be in the context. From the repo root:

```bash
docker build -t go-custom-signals -f go/example/go-custom-signals/Dockerfile go/
docker build -t go-api            -f go/example/go-api/Dockerfile go/
docker build -t go-backend        -f go/example/go-backend/Dockerfile go/
docker build -t go-process        -f go/example/go-process/Dockerfile go/
```

`go-api`, `go-backend` and `go-process` also have a `Dockerfile.demo`: the same
build, used by the CI `example-images` job (the .NET and Python examples use
`Dockerfile.demo` to consume a prebuilt package; Go always builds from source,
so the two files differ only in comments).

## Run against a Collector

The repo's test Collector (`otel/opentelemetry-collector-contrib` with the
`debug` exporter at `verbosity: detailed`, config in
[`testing/e2e/collector.yaml`](https://github.com/StaffOps/otel-libs/blob/main/testing/e2e/collector.yaml))
prints everything it receives:

```bash
docker compose -f testing/docker-compose.yml --profile e2e up -d collector
NET=otel-libs-testing_default   # the compose project's network

docker run --rm --network $NET \
  -e OTEL_SERVICE_NAME=go-custom-signals \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  go-custom-signals

# The Collector log keeps every earlier run: filter on the printed trace_id
# (or recreate the Collector first with `up -d --force-recreate collector`).
docker compose -f testing/docker-compose.yml --profile e2e logs --no-log-prefix --tail 2000 collector \
  | grep -B 4 -A 30 <trace_id>
docker compose -f testing/docker-compose.yml --profile e2e stop collector   # when done
```

The API, backend and worker (explicit `-e` flags: a `COMMON="-e … -e …"`
string is not word-split by zsh):

```bash
docker run -d --rm --name go-backend --network $NET \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  -e OTEL_SERVICE_NAME=go-backend go-backend
docker run -d --rm --name go-api --network $NET -p 8080:8080 \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  -e OTEL_SERVICE_NAME=go-api -e BACKEND_URL=http://go-backend:50051 go-api
docker run -d --rm --name go-process --network $NET \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  -e OTEL_SERVICE_NAME=go-process go-process

curl -X POST http://localhost:8080/detect -H 'Content-Type: application/json' -d '{"source":"api"}'
curl http://localhost:8080/status/det-1
curl http://localhost:8080/metrics/summary
curl http://localhost:8080/health          # counted in http.server.request.duration, no span

docker stop go-api go-backend go-process   # SIGTERM: drained (≤ 4 s) and flushed (≤ 5 s)
```

Each program's shutdown budget fits inside `docker stop`'s default 10 s grace
period; if you raise it, raise the grace period too (`docker stop -t 30 …`).

Without `OTEL_EXPORTER_OTLP_ENDPOINT` nothing is exported over OTLP: logs are
written to stdout as JSON and metrics are served on `:9464/metrics`.

---

## go-custom-signals

[`go-custom-signals/main.go`](https://github.com/StaffOps/otel-libs/blob/main/go/example/go-custom-signals/main.go) processes a batch of
three simulated orders (`ord-3` fails) and exits. Its Python twin,
[`python-custom-signals`](https://github.com/StaffOps/otel-libs/blob/main/python/example/python-custom-signals/custom_signals.py),
uses the same names and the same trace shape, so the two outputs can be
compared side by side.

| Signal | Code |
|--------|------|
| Root span | `otelhelper.StartRootSpan(ctx, tracer, "orders.process-batch", …)` with `batch.size`; `SetStatus(codes.Error, "1 of 3 orders failed")` when an order failed |
| Child spans | `tracer.Start(ctx, "order.process", …)` per order with `order.id`, `order.type`; event `order.validated` (`order.amount`); on failure `RecordError(err)` + `SetStatus(codes.Error, "payment declined")` |
| Error type | `*PaymentDeclinedError`, returned unwrapped, so `exception.type` is `*main.PaymentDeclinedError` on the span and on the log record |
| Counter | `order.processed` (`{order}`), attributes `order.type` and, on failure only, `error.type` |
| Histogram | `order.duration` (`s`, explicit buckets 5 ms – 1 s), same attributes |
| Up-down counter | `order.active` (`{order}`, `order.type`), `+1` / `-1` around each order |
| Observable gauge | `order.queue.depth` (`{order}`, `queue.name=orders`), read by a callback |
| Logs | `slog.New(otelhelper.NewSlogHandler())`; `InfoContext` / `ErrorContext` with the span's `ctx`; the `err` attribute becomes `exception.type` / `exception.message` |

The loop checks `ctx.Err()` before each order: on `SIGTERM` or Ctrl-C it stops
taking orders and still flushes.

The [Collector run](#run-against-a-collector) above prints `trace_id=<id>`;
filter the Collector log on it.

### Expected output without a Collector

```bash
docker run --rm -e OTEL_SERVICE_NAME=go-custom-signals go-custom-signals
```

Recorded on 2026-10-28, untrimmed:

```
time=2026-10-28T18:41:25.657Z level=WARN msg="otelhelper: deployment.environment.name is not set; set it in OTEL_RESOURCE_ATTRIBUTES (e.g. deployment.environment.name=PRD); log level defaults to INFO" using=absent
trace_id=ed9b11fd713e179414bfb90b64e6dd2f
{"time":"2026-10-28T18:41:25.693Z","level":"INFO","msg":"order processed","order.id":"ord-1","order.amount":42.5}
{"time":"2026-10-28T18:41:25.714Z","level":"INFO","msg":"order processed","order.id":"ord-2","order.amount":99.9}
{"time":"2026-10-28T18:41:25.734Z","level":"ERROR","msg":"order failed","order.id":"ord-3","exception":"card declined by issuer"}
{"time":"2026-10-28T18:41:25.735Z","level":"INFO","msg":"batch finished","batch.size":3,"orders.failed":1}
```

The first line is a helper diagnostic (stderr). The JSON lines are the stdout
fallback: no trace IDs, the error attribute rendered as `exception`. Traces are
not exported, and metrics are served on `:9464/metrics` only while the process
runs.

### What the exported telemetry contains

To see the spans, metrics and log records without a Collector, switch the
exporters to the console (no endpoint needed; output is one JSON document per
batch):

```bash
docker run --rm -e OTEL_SERVICE_NAME=go-custom-signals \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_TRACES_EXPORTER=console -e OTEL_METRICS_EXPORTER=console -e OTEL_LOGS_EXPORTER=console \
  go-custom-signals
```

Condensed from a run on 2026-10-28 (trace `45fe7c3516492acc296a9113e2a60e44`;
IDs differ on every run). Spans:

```
orders.process-batch  26299b17894dd960  no parent   Error "1 of 3 orders failed"  batch.size=3
├── order.process     02b2799c7db33272  ord-1 standard  Unset  event order.validated (order.amount=42.5)
├── order.process     32706bd3b2a4e771  ord-2 express   Unset  event order.validated (order.amount=99.9)
└── order.process     ec4a689e7efdbafb  ord-3 standard  Error "payment declined"
                      events order.validated (order.amount=10),
                             exception (exception.type=*main.PaymentDeclinedError,
                                        exception.message="card declined by issuer")
```

Metrics (scope `orders`; every point recorded inside a span carries an
exemplar with its trace and span ID):

| Instrument | Data points |
|------------|-------------|
| `order.processed` (`{order}`) | `order.type=standard` 1; `order.type=express` 1; `order.type=standard, error.type=PaymentDeclinedError` 1 |
| `order.duration` (`s`) | one histogram point per attribute set above, count 1, sum ≈ 0.021–0.038 |
| `order.active` (`{order}`) | `order.type=standard` 0; `order.type=express` 0 |
| `order.queue.depth` (`{order}`) | `queue.name=orders` 0 |

Log records (scope `otel-helper`, the slog bridge), all with trace
`45fe7c3516492acc296a9113e2a60e44`:

| Body | Attributes | Span ID |
|------|------------|---------|
| `order processed` | `order.id=ord-1`, `order.amount=42.5` | `02b2799c7db33272` (its `order.process`) |
| `order processed` | `order.id=ord-2`, `order.amount=99.9` | `32706bd3b2a4e771` |
| `order failed` | `order.id=ord-3`, `exception.message=card declined by issuer`, `exception.type=*main.PaymentDeclinedError` | `ec4a689e7efdbafb` (the failed span) |
| `batch finished` | `batch.size=3`, `orders.failed=1` | `26299b17894dd960` (the root span) |

The resource of every signal is `service.name`, `deployment.environment.name`,
`service.instance.id` and the SDK's `telemetry.sdk.*`. The Collector's `debug`
exporter shows the same data in its own text format.

---

## go-api, go-backend, go-process

```
go-process (background worker, independent of the other two; :8080 /health only)
  ├── anomalyScanner (30s)   → root span process.anomaly_scan, correlated log
  ├── alertDispatcher (60s)  → root span process.alert_dispatch
  └── cleanupWorker (3m)     → root span process.cleanup

go-api (HTTP :8080)
  ├── POST /detect           → calls go-backend
  ├── GET /status/{id}       → calls go-backend
  ├── GET /metrics/summary   → business counters
  └── GET /health            → counted in metrics, no span
         │ HTTP (traceparent)
         ▼
go-backend (HTTP :50051 — a net/http server; the port is historical)
  ├── POST /detect           → simulated detection (50–500 ms)
  ├── GET /status/{id}
  └── GET /health
```

### Traces

Recorded from the Collector (otelhttp v0.72: server spans `<METHOD> <route>`,
client spans `HTTP <METHOD>`):

```
POST /detect                  (go-api, Server)
└── detect.submit             (go-api, Internal)
    └── HTTP POST             (go-api, Client)
        └── POST /detect      (go-backend, Server)
            └── backend.detect

GET /status/{id}              (go-api, Server)
└── detect.status             (go-api, Internal)
    └── HTTP GET              (go-api, Client)
        └── GET /status/{id}  (go-backend, Server)
            └── backend.get_status
```

`go-process` starts an independent trace per iteration (`process.anomaly_scan`
every 30 s, `process.alert_dispatch` every 60 s, `process.cleanup` every 3 min).

### Metrics

> **Legacy names — do not copy.** The api/backend/process metric names below
> (`*.total` / `*_total`, `*_ms`, no unit, the `process.*` namespace) predate the naming
> conventions. They are kept because the shared dashboards and queries in
> `dashboards/` use them. For new instruments follow
> [`go-custom-signals`](#go-custom-signals): dot-separated names, the unit in
> `WithUnit`, seconds for durations, no `_total` suffix.

| Service | Instruments |
|---------|-------------|
| go-api | `api.requests.total` (counter, `endpoint`), `api.request.duration_ms` (histogram), plus `http.server.request.duration` / `http.client.request.duration` |
| go-backend | `backend.processing.duration_ms` (histogram), plus `http.server.request.duration` |
| go-process | `process.metrics.scanned`, `process.alerts.dispatched` (counters), `process.alerts.pending` (observable gauge) |
| all | Go runtime metrics (`go.memory.used`, `go.goroutine.count`, …) |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_SERVICE_NAME` | _(unset → `unknown_service:<exe>`)_ | Service name |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | `deployment.environment.name=DEV` etc.; unset → attribute absent, log level INFO |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset → no OTLP export)_ | Collector endpoint, e.g. `http://collector:4317` |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | DEBUG logs + `debug=true` on entry spans |
| `BACKEND_URL` | `http://localhost:50051` | Backend URL (go-api) |

Every other variable in the [README](../README.md#environment-variables)
applies. `SERVICE_NAME` and `ENVIRONMENT` still work but are deprecated (warned).

## File structure

```
go/example/
├── go-custom-signals/
│   ├── main.go          # custom spans, metrics and correlated logs
│   ├── Dockerfile
│   └── go.mod
├── go-api/
│   ├── main.go          # HTTP API with context propagation
│   ├── Dockerfile
│   ├── Dockerfile.demo  # same build, used by CI (example-images)
│   └── go.mod
├── go-backend/          # main.go, Dockerfile, Dockerfile.demo, go.mod
├── go-process/          # main.go, Dockerfile, Dockerfile.demo, go.mod
├── protos/
│   └── anomaly.proto    # reference proto (not compiled)
└── README.md
```
