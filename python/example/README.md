# Python examples

Runnable apps showing `otel-helper` in use. Audience: developers who want to
see the helper's output before instrumenting their own service.

| Example | Shows |
|---|---|
| [`python-custom-signals/`](#custom-signals) | custom spans, metrics and logs correlated with the trace — a short job that exits; same names and trace shape as the Go twin |
| `python-api/`, `python-backend/`, `python-process/` | a notification service: FastAPI → gRPC distributed tracing, workers with root spans, health probes, graceful shutdown |

Per-endpoint telemetry and Grafana queries for the notification service:
[EXAMPLES.md](EXAMPLES.md).

Prerequisites: Docker. Commands run from the repository root.

---

## Custom signals

[`python-custom-signals/custom_signals.py`](https://github.com/StaffOps/otel-libs/blob/main/python/example/python-custom-signals/custom_signals.py)
processes a batch of three simulated orders (`ord-3` fails) and exits. Its Go
twin, [`go-custom-signals`](https://github.com/StaffOps/otel-libs/blob/main/go/example/go-custom-signals/main.go),
uses the same names and the same trace shape, so the two outputs can be
compared side by side.

| Signal | What the code does |
|---|---|
| Root span | `start_root_span(tracer, "orders.process-batch", attributes={"batch.size": 3})`; `set_status(ERROR, "1 of 3 orders failed")` when an order failed |
| Child spans | `tracer.start_as_current_span("order.process")` per order with `order.id`, `order.type`; event `order.validated` (`order.amount`); on failure `record_exception(e)` + `set_status(ERROR, "payment declined")` |
| Error type | `PaymentDeclinedError`: `exception.type` is `__main__.PaymentDeclinedError` on the span and `PaymentDeclinedError` on the log record (the logging handler uses the bare class name) |
| Counter | `order.processed` (`{order}`), attributes `order.type` and, on failure only, `error.type` |
| Histogram | `order.duration` (`s`, explicit buckets 5 ms – 1 s), same attributes |
| Up-down counter | `order.active` (`{order}`, `order.type`), `+1` / `-1` around each order |
| Observable gauge | `order.queue.depth` (`{order}`, `queue.name=orders`), read by a callback |
| Logs | `logger.info(...)` / `logger.error(..., exc_info=e)` inside the spans: each record carries the current span's trace and span IDs; `exc_info` adds `exception.type` / `exception.message` / `exception.stacktrace` |

SIGTERM and SIGINT set a stop flag that the loop checks before each order, so
the batch stops early and `shutdown_telemetry()` still flushes.

### Run it against a Collector

The repo's test Collector (`testing/docker-compose.yml`, debug exporter with
`verbosity: detailed`) prints everything it receives. From the **repository
root**:

```bash
docker compose -f testing/docker-compose.yml --profile e2e up -d collector

docker build -t python-custom-signals -f python/example/python-custom-signals/Dockerfile python
docker run --rm --network otel-libs-testing_default \
  -e OTEL_SERVICE_NAME=python-custom-signals \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  python-custom-signals

# The Collector log keeps every earlier run: filter on the printed trace_id
# (or recreate the Collector first with `up -d --force-recreate collector`).
docker compose -f testing/docker-compose.yml --profile e2e logs --no-log-prefix --tail 2000 collector \
  | grep -B 4 -A 30 <trace_id>
docker compose -f testing/docker-compose.yml --profile e2e stop collector
```

The job flushes everything through `shutdown_telemetry()` before it exits, so
all three signals arrive in one batch each. `OTEL_SERVICE_NAME` is required to
see a real service name (unset → `unknown_service` and a warning).

On stderr you may see one upstream line, `HTTPXClientInstrumentor only
instruments "httpx >= 0.18.0", but no installed version was found`: the image
has no httpx; it is harmless.

### Without a Collector

```bash
docker run --rm -e OTEL_SERVICE_NAME=python-custom-signals python-custom-signals
```

Recorded on 2026-10-28, untrimmed:

```text
[OtelHelper] deployment.environment.name is not set (OTEL_RESOURCE_ATTRIBUTES, or the legacy ENVIRONMENT alias): the attribute is omitted and the log level defaults to INFO.
OTel Prometheus exporter: metrics available at http://0.0.0.0:9464/metrics
HTTPXClientInstrumentor only instruments "httpx >= 0.18.0", but no installed version was found, so nothing can be instrumented.
trace_id=2114909b129a5edb226c04c4c6965aaf
{"time": "2026-10-28T18:41:26.739Z", "level": "INFO", "msg": "order processed", "logger": "custom_signals", "order.id": "ord-1", "order.amount": 42.5}
{"time": "2026-10-28T18:41:26.760Z", "level": "INFO", "msg": "order processed", "logger": "custom_signals", "order.id": "ord-2", "order.amount": 99.9}
{"time": "2026-10-28T18:41:26.782Z", "level": "ERROR", "msg": "order failed", "logger": "custom_signals", "order.id": "ord-3", "exception": "Traceback (most recent call last):\n  File \"/app/custom_signals.py\", line 130, in main\n    charge(order)\n  File \"/app/custom_signals.py\", line 81, in charge\n    raise PaymentDeclinedError(\"card declined by issuer\")\nPaymentDeclinedError: card declined by issuer"}
{"time": "2026-10-28T18:41:26.782Z", "level": "INFO", "msg": "batch finished", "logger": "custom_signals", "batch.size": 3, "orders.failed": 1}
```

The first three lines are diagnostics (stderr). The JSON lines are the stdout
fallback: no trace IDs (the program prints the trace ID itself, as the Go twin
does). Traces are not exported, and metrics are served on `:9464/metrics` only
while the process runs; there the instruments appear as
`order_processed_total`, `order_duration_seconds_bucket`, `order_active` and
`order_queue_depth`.

### What the exported telemetry contains

To see the spans, metrics and log records without a Collector, switch the
exporters to the console (no endpoint needed):

```bash
docker run --rm -e OTEL_SERVICE_NAME=python-custom-signals \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_TRACES_EXPORTER=console -e OTEL_METRICS_EXPORTER=console -e OTEL_LOGS_EXPORTER=console \
  python-custom-signals
```

Condensed from a run on 2026-10-28 (trace `90eba5d2358b21b30446d62acfb5c4bd`;
IDs differ on every run). Spans:

```text
orders.process-batch  83295ff7022e6302  no parent   Error "1 of 3 orders failed"  batch.size=3
├── order.process     19d681aa6b89f24b  ord-1 standard  Unset  event order.validated (order.amount=42.5)
├── order.process     ca364c82b554ad15  ord-2 express   Unset  event order.validated (order.amount=99.9)
└── order.process     66a61162c7e890a7  ord-3 standard  Error "payment declined"
                      events order.validated (order.amount=10),
                             exception (exception.type=__main__.PaymentDeclinedError,
                                        exception.message="card declined by issuer",
                                        exception.stacktrace=…)
```

Metrics (scope `orders`; every point recorded inside a span carries an
exemplar with its trace and span ID):

| Instrument | Data points |
|------------|-------------|
| `order.processed` (`{order}`) | `order.type=standard` 1; `order.type=express` 1; `order.type=standard, error.type=PaymentDeclinedError` 1 |
| `order.duration` (`s`) | one histogram point per attribute set above, count 1, sum ≈ 0.020–0.022 |
| `order.active` (`{order}`) | `order.type=standard` 0; `order.type=express` 0 |
| `order.queue.depth` (`{order}`) | `queue.name=orders` 0 |

The same batch carries the helper's automatic `system.*` and `process.*`
metrics (scope `opentelemetry.instrumentation.system_metrics`).

Log records, all with trace `90eba5d2358b21b30446d62acfb5c4bd`:

| Body | Attributes | Span ID |
|------|------------|---------|
| `order processed` | `order.id=ord-1`, `order.amount=42.5` | `19d681aa6b89f24b` (its `order.process`) |
| `order processed` | `order.id=ord-2`, `order.amount=99.9` | `ca364c82b554ad15` |
| `order failed` | `order.id=ord-3`, `exception.type=PaymentDeclinedError`, `exception.message=card declined by issuer`, `exception.stacktrace` | `66a61162c7e890a7` (the failed span) |
| `batch finished` | `batch.size=3`, `orders.failed=1` | `83295ff7022e6302` (the root span) |

The resource of every signal is `service.name`, `deployment.environment.name`,
`service.instance.id` and the SDK's `telemetry.sdk.*`. The Collector's `debug`
exporter shows the same data in its own text format.

---

## Notification service

```text
python-process (workers)
  ├── queue consumer (30 s)  → POST /notify
  ├── retry worker (60 s)    → POST /notify
  ├── cleanup worker (3 min)
  └── health checker (60 s)  → GET /health
         │ HTTP :8000
         ▼
python-api (FastAPI)
  ├── POST /notify          ├── GET /templates
  ├── GET /notify/{id}      ├── POST /templates
  ├── GET /metrics/summary  └── GET /health
         │ gRPC :50051
         ▼
python-backend (gRPC server)
  ├── SendNotification → render + deliver (email/sms/push, 10% failures)
  ├── GetStatus
  ├── RenderTemplate
  └── grpc.health.v1.Health
```

What each app demonstrates:

- **python-api**: `import otel_helper  # isort: skip` first; `FastAPI` imported
  before setup, so `instrument_fastapi(app)`; `/health` has no server span but
  counts in `http.server.request.duration`; `shutdown_telemetry()` in the
  FastAPI lifespan (uvicorn turns SIGTERM into a lifespan shutdown).
- **python-backend**: `grpc.aio` server instrumented automatically, standard
  health service not traced, custom child spans and a histogram with explicit
  buckets, SIGTERM and SIGINT handled in the event loop (`add_signal_handler`,
  registered before `server.start()` → graceful `server.stop(grace=4)` →
  `shutdown_telemetry(timeout_ms=5_000)`).
- **python-process**: `start_root_span()` per worker cycle, manual
  `traceparent` injection into httpx headers, SIGTERM/SIGINT →
  `loop.add_signal_handler` sets an `asyncio.Event` that cancels the workers →
  `shutdown_telemetry()`. Also the program the e2e smoke runs
  (`testing/e2e/smoke.sh`).

### Build

From the repository root (the build context is `python/`):

```bash
docker build -t python-api     -f python/example/python-api/Dockerfile python
docker build -t python-backend -f python/example/python-backend/Dockerfile python
docker build -t python-process -f python/example/python-process/Dockerfile python
```

`Dockerfile` installs the helper from the local source; `Dockerfile.demo`
installs a prebuilt wheel from `packages/` (the CI artifact) instead.

### Run

The three containers need one network and, to export, a Collector on it. With
the repo's test Collector (started as in [Custom signals](#run-it-against-a-collector)),
use its network:

Explicit `-e` flags: a `COMMON="-e … -e …"` string is not word-split by zsh.

```bash
NET=otel-libs-testing_default

docker run -d --rm --name python-backend --network $NET \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  -e OTEL_SERVICE_NAME=python-backend python-backend
docker run -d --rm --name python-api --network $NET -p 8000:8000 \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  -e OTEL_SERVICE_NAME=python-api -e BACKEND_URL=python-backend:50051 python-api
docker run -d --rm --name python-process --network $NET \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  -e OTEL_SERVICE_NAME=python-process -e API_URL=http://python-api:8000 python-process
```

Without `OTEL_EXPORTER_OTLP_ENDPOINT` the apps run with the Prometheus
fallback (`:9464/metrics`) and JSON logs on stdout. Stop with
`docker stop python-process python-api python-backend` (SIGTERM; telemetry is
flushed within `docker stop`'s default 10 s grace period).

> **Legacy names — do not copy.** The metric names of these three apps
> (`*_total`, `*_seconds` in the name, no `unit`, plural `notifications.*`
> namespaces) predate the naming conventions. They are kept because the shared
> dashboards and the queries in [EXAMPLES.md](EXAMPLES.md) use them. For new
> instruments follow [Custom signals](#custom-signals): dot-separated names, the
> unit in `unit=`, seconds for durations, no `_total` suffix.

### Try it

```bash
curl -X POST http://localhost:8000/notify \
  -H "Content-Type: application/json" \
  -d '{"recipient": "user@example.com", "channel": "email", "template_id": "welcome", "variables": {"name": "User", "product": "MyApp"}}'

curl http://localhost:8000/notify/<notification_id>
curl http://localhost:8000/templates
curl http://localhost:8000/health
```

### Trace of `POST /notify`

Recorded against the test Collector on 2026-10-28 (one trace id across both
services):

```text
POST /notify                                          Server   python-api
├── POST /notify http receive                         Internal
├── api.send-notification                             Internal
│   └── /notification.NotificationService/SendNotification   Client
│       └── /notification.NotificationService/SendNotification   Server   python-backend
│           └── backend.send-notification             Internal
│               ├── backend.render-template           Internal
│               └── backend.deliver                   Internal
└── POST /notify http send  (×2)                      Internal
```

The `Notification sent` log record of python-api carries the trace id of this
trace and the span id of `api.send-notification`. `GET /health` produced no
span, and appeared in `http.server.request.duration` with
`http.route=/health`, `http.response.status_code=200`.

A worker cycle is its own trace:

```text
process.queue-consume  (root)
└── process.dispatch-notification
    └── httpx client span (traceparent injected) → POST /notify on python-api → (trace above)
```
