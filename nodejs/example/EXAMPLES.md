# Node.js examples — telemetry reference

What each example emits, span by span and metric by metric, with ready-to-use
queries (Tempo TraceQL, PromQL). Audience: developers building dashboards or
checking that their own instrumentation looks like the examples. How to run
the examples: [README.md](README.md). The default image build runs the
published helper the examples pin (their `package.json`); a `HELPER_SOURCE=workspace`
build runs this branch's, whose telemetry may differ where the branch changed
it (see the CHANGELOG). Where Node differs from the Python and
Go twins, the last section, [Differences from Python and Go](#differences-from-python-and-go),
lists it.

Query conventions:

- Metric names assume the usual OTLP → Prometheus translation: dots become
  underscores, the unit is appended (`_seconds`), counters get `_total`
  (never twice: `notifications.sent_total` stays `notifications_sent_total`).
- `service_name` is the resource attribute `service.name` promoted to a label
  (VictoriaMetrics OTLP ingestion does this; with `target_info`-based backends
  join on it instead). On the Prometheus fallback (`:9464/metrics`) there is
  no `service_name` label: use the scrape job's labels.
- HTTP metrics use the stable semantic conventions: the Node HTTP
  instrumentations emit only those (an `OTEL_SEMCONV_STABILITY_OPT_IN` value
  asking for the old ones is warned and has no HTTP effect).

The tables below were checked against collector-contrib 0.160.0 (debug
exporter, `verbosity: detailed`) on 2026-11-03, Node 24.

---

## Custom signals (`node-custom-signals`)

Same names as the Go and Python twins (`go-custom-signals`,
`python-custom-signals`): the span, metric and log names, attribute keys,
units, instrument kinds and trace shape match.

| Signal | Name | Kind | Attributes |
|---|---|---|---|
| span | `orders.process-batch` | root, Internal (`startRootSpan`) | `batch.size`; status ERROR `1 of 3 orders failed` when an order fails |
| span | `order.process` | child, one per order (`tracer.startActiveSpan`) | `order.id`, `order.type`; event `order.validated` (`order.amount`); on failure event `exception` (`exception.type` `PaymentDeclinedError`, `exception.message`, `exception.stacktrace`) + status ERROR `payment declined` |
| metric | `order.processed` (`{order}`) | Counter, integer | `order.type`; `error.type` on failure only |
| metric | `order.duration` (`s`) | Histogram, buckets 5 ms – 1 s (`advice.explicitBucketBoundaries`) | `order.type`; `error.type` on failure only |
| metric | `order.active` (`{order}`) | UpDownCounter, integer | `order.type` |
| metric | `order.queue.depth` (`{order}`) | Observable gauge, integer | `queue.name` |
| log | `order processed` (INFO) | correlated with its `order.process` | `order.id`, `order.amount` |
| log | `order failed` (ERROR) | correlated with the failed `order.process` | `order.id`, `exception.type`, `exception.message`, `exception.stacktrace` |
| log | `batch finished` (INFO) | correlated with `orders.process-batch` | `batch.size`, `orders.failed` |

Tracer and meter scope: `orders`. Log scope: `node-custom-signals` (the
`createLogger({ name })` value; per language: design D-JS14 row "Log instrumentation scope per language", 2026-11-03).

How the code produces them
([`node-custom-signals/custom-signals.js`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/example/node-custom-signals/custom-signals.js)):

| Signal | What the code does |
|---|---|
| Root span | `startRootSpan('orders.process-batch', fn, { tracer, attributes: { 'batch.size': 3 } })`; `batch.setStatus({ code: ERROR, message: '1 of 3 orders failed' })` inside `fn` when an order failed |
| Child spans | `tracer.startActiveSpan('order.process', { attributes }, fn)` per order; `span.addEvent('order.validated', …)`; on failure `span.recordException(err)` + `span.setStatus({ code: ERROR, message: 'payment declined' })` |
| Error type | `class PaymentDeclinedError extends Error` that sets `this.name`: `exception.type` is the error's `name` on the span and on the log record (without `this.name` it reads `Error`). An error that has a `code` (an errno string such as `ECONNREFUSED`, a gRPC status number) gets the **code** as `exception.type` from the SDK, on spans and log records alike: record such errors as a plain `{ name, message, stack }`, as node-process's `recordError()` does (2026-11-03) |
| Counter | `meter.createCounter('order.processed', { unit: '{order}', valueType: ValueType.INT })` |
| Histogram | `meter.createHistogram('order.duration', { unit: 's', advice: { explicitBucketBoundaries: [0.005, …, 1] } })` |
| Up-down counter | `meter.createUpDownCounter('order.active', …)`, `+1` / `-1` around each order |
| Observable gauge | `meter.createObservableGauge('order.queue.depth', …)` + `addCallback((result) => result.observe(depth, { 'queue.name': 'orders' }))` |
| Logs | `logger.info(...)` / `logger.error('order failed', { 'order.id': id, error: err })` inside the spans: each record carries the active span's trace and span IDs; the first `Error`-valued attribute becomes the record's `exception.type` / `exception.message` / `exception.stacktrace` (it is not written as an attribute of its own) |

`valueType: ValueType.INT` keeps the counts integers, as Go's `Int64*`
instruments and Python's `int` adds; without it the JS SDK exports every
number as a double (`Value: 1.000000` instead of `Value: 1`; design D-JS14 row "Integer instruments need `ValueType.INT`", 2026-11-03). The notification apps pass it too.

`order.queue.depth` is an observable **gauge** on purpose, as in Go and
Python: it reports the current depth. An asynchronous up-down counter
(`createObservableUpDownCounter`) would be the additive alternative, summable
across instances; the twins keep the gauge (2026-11-03).

SIGTERM and SIGINT set a stop flag that the loop checks before each order
(`batch interrupted` WARN, `orders.pending`), so the batch stops early and
`shutdown(5000)` still flushes; then the job calls `process.exit()`.

Recorded run (trace `9c936622d16d5ac3a547cdd15eb89a55`, 2026-11-03 after the fix
round; IDs differ on every run), condensed from the Collector's debug output:

```text
orders.process-batch  no parent  Internal  Error "1 of 3 orders failed"  batch.size=3
├── order.process  ord-1 standard  Unset  event order.validated (order.amount=Double(42.5))
├── order.process  ord-2 express   Unset  event order.validated (order.amount=Double(99.9))
└── order.process  ord-3 standard  Error "payment declined"
      event order.validated (order.amount=Double(10.5))
      event exception (exception.type=PaymentDeclinedError, exception.message, exception.stacktrace)

logs (scope node-custom-signals), each with the trace ID above:
  INFO  order processed  order.id=ord-1 order.amount=42.5   span = ord-1's order.process
  INFO  order processed  order.id=ord-2 order.amount=99.9   span = ord-2's order.process
  ERROR order failed     order.id=ord-3 exception.type=PaymentDeclinedError …   span = ord-3's order.process
  INFO  batch finished   batch.size=3 orders.failed=1       span = orders.process-batch

metrics (scope orders, final collection at shutdown):
  order.queue.depth  Gauge                     {queue.name=orders}                         0
  order.active       Sum, not monotonic        {order.type=standard} 0, {order.type=express} 0
  order.duration     Histogram, unit s         {standard}, {express}, {standard, error.type=PaymentDeclinedError}
  order.processed    Sum, monotonic, {order}   the same three attribute sets, 1 each
```

```traceql
{ resource.service.name = "node-custom-signals" && name = "order.process" && status = error }
```

```traceql
{ resource.service.name = "node-custom-signals" && name = "orders.process-batch" } >> { event:name = "exception" }
```

The PromQL names below are the helper's `/metrics` names (checked on a scrape
of the running job: `order_processed_total`, `order_duration_seconds_bucket`,
`order_active`, `order_queue_depth`); over OTLP they depend on the backend's
naming settings.

```promql
sum by (error_type) (rate(order_processed_total{service_name="node-custom-signals"}[5m]))
histogram_quantile(0.95, sum by (le) (rate(order_duration_seconds_bucket{service_name="node-custom-signals"}[5m])))
order_queue_depth{service_name="node-custom-signals", queue_name="orders"}
```

```logql
{service_name="node-custom-signals"} | trace_id = "<trace_id printed by the job>"
```

(The LogQL form depends on how the backend stores trace ids; in Loki's OTLP
ingestion they are structured metadata named `trace_id` / `span_id`.)

---

## Notification service — metrics

| Metric (instrument name) | Type | Attributes | Emitted by |
|---|---|---|---|
| `notifications.sent_total` | Counter | `channel` | node-api, per successful `POST /notify` |
| `notifications.failed_total` | Counter | `channel` | node-api, per `POST /notify` the backend reported failed |
| `templates.rendered_total` | Counter | — | node-api, per `POST /templates` |
| `notifications.active_requests` | UpDownCounter | — | node-api, in-flight `POST /notify` |
| `notification.delivery_duration_seconds` | Histogram (seconds, buckets 10 ms – 2.5 s) | `channel` | node-backend, per delivery |
| `notification.delivery_attempts_total` | Counter | `channel`, `success` (`True` / `False`, as Python) | node-backend |
| `notifications.queue_depth` | UpDownCounter | — | node-process |
| `notifications.retries_total` | Counter | — | node-process |
| `notifications.cleaned_total` | Counter | — | node-process |
| `http.server.request.duration` | Histogram (s) | `http.request.method`, `http.route` (express routes only), `http.response.status_code`, `url.scheme`, `network.protocol.version` | node-api (automatic, health probes included); node-process's probe server on `:8080` (no `http.route`: a plain `node:http` server) |
| `http.client.request.duration` | Histogram (s) | `http.request.method`, `server.address`, `server.port`, `http.response.status_code`, `url.scheme`, `network.protocol.version` | node-process (automatic, `fetch`; `/health` calls not counted) |

The instrument names are the legacy ones of the Python and .NET examples (the
shared dashboards read them); new instruments follow
[Custom signals](#custom-signals-node-custom-signals). The gRPC
instrumentation emits **no RPC metrics**, and Node emits **no runtime
metrics** (`system.*` / `process.*` in Python, `go.*` in Go).

---

## 1. node-api (express :8000)

Every request gets an automatic server span named `<METHOD> <route>`
(`POST /notify`, `GET /notify/:notificationId`) plus an express
`request handler - <route>` internal span (`http.route`, `express.name`,
`express.type=request_handler`), and is counted in
`http.server.request.duration`. A request that matches no route (404) or
fails before the route (malformed JSON, 400) has a server span named after
the method alone (`GET`, `POST`) and no `http.route`.

### 1.1 POST /notify

Sends a notification through the backend.

```text
POST /notify                              (server span)
└── request handler - /notify
    └── api.send-notification
        ├── notifications.active_requests +1 / -1
        ├── gRPC client notification.NotificationService/SendNotification → node-backend
        └── notifications.sent_total{channel} or notifications.failed_total{channel}
```

| Span | Attributes |
|---|---|
| `api.send-notification` | `notification.channel`, `notification.recipient`, `notification.template`; status ERROR `Backend reported failure` on failure |

Log record `Notification sent` (`notification_id`, `status`) is correlated with
`api.send-notification`.

```traceql
{ resource.service.name = "node-api" && name = "api.send-notification" && span.notification.channel = "email" }
```

```promql
notifications_active_requests{service_name="node-api"}
sum by (channel) (rate(notifications_sent_total{service_name="node-api"}[5m]))
histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="node-api", http_route="/notify", http_request_method="POST"}[5m])))
```

### 1.2 GET /notify/:notificationId

Returns the status from the API's store, or asks the backend (`GetStatus`).

| Span | Attributes |
|---|---|
| `api.get-status` | `notification.id` |

```traceql
{ resource.service.name = "node-api" && name = "api.get-status" && span.notification.id = "866bbcb5" }
```

The route label is express's template, `/notify/:notificationId`:

```promql
histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="node-api", http_route="/notify/:notificationId", http_request_method="GET"}[5m])))
```

### 1.3 GET /templates and GET /metrics/summary

No custom span: the automatic server span and the express handler span only.

```traceql
{ resource.service.name = "node-api" && span.http.route = "/templates" && span.http.request.method = "GET" }
```

```promql
sum(rate(http_server_request_duration_seconds_count{service_name="node-api", http_route="/templates"}[5m]))
```

### 1.4 POST /templates

| Span | Attributes |
|---|---|
| `api.create-template` | `template.name` |

Increments `templates.rendered_total`.

```promql
rate(templates_rendered_total{service_name="node-api"}[5m])
```

### 1.5 GET /health

**No span** (health sampler), but **counted** in the HTTP server metrics, so
failing probes are visible:

```promql
sum(rate(http_server_request_duration_seconds_count{service_name="node-api", http_route="/health", http_response_status_code=~"5.."}[5m]))
```

Exclude probes from SLIs:

```promql
sum(rate(http_server_request_duration_seconds_count{service_name="node-api", http_route!~"/health|/healthz|/ready.*|/live.*|/ping|/metrics"}[5m]))
```

### 1.6 Error responses

| Case | Status | Body |
|---|---|---|
| Invalid body (a field missing or of the wrong type, an explicit `null` included) | 422 | `{"detail": "<field>: …"}` — one string, e.g. `{"detail":"recipient: field required (string)"}` |
| Malformed JSON | 400 | `{"detail": "Bad Request"}` |
| Unknown route | 404 | `{"detail": "Not Found"}` |
| Unexpected error | 500 | `{"detail": "Internal Server Error"}`, after a `Request failed` ERROR log record (`exception.type`, `exception.message`) |

A 4xx/5xx `detail` is the fixed status text, never the error's own message.

---

## 2. node-backend (gRPC :50051)

Every RPC gets an automatic server span named
`notification.NotificationService/<Method>` (attributes `rpc.system`,
`rpc.service`, `rpc.method`, `rpc.grpc.status_code`), child of the caller's
client span (same name, kind Client, plus `server.address` / `server.port`).
`grpc.health.v1.Health/*` calls are not traced. A handler that fails answers
gRPC `UNKNOWN`.

### 2.1 SendNotification

```text
notification.NotificationService/SendNotification   (server span)
└── backend.send-notification
    ├── backend.render-template
    └── backend.deliver
        ├── notification.delivery_duration_seconds{channel}
        └── notification.delivery_attempts_total{channel, success}
```

| Span | Attributes |
|---|---|
| `backend.send-notification` | `notification.id`, `notification.channel`, `notification.recipient` |
| `backend.render-template` | — |
| `backend.deliver` | `notification.provider`; status ERROR `Provider timeout` on the simulated 10% failures |

Log record `Notification processed` (`notification_id`, `status`, `channel`).

The backend does not watch the caller's deadline: when node-api's 5 s
deadline expires mid-call, the handler still finishes, its spans end and are
exported, and the reply is dropped (as python-backend).

```traceql
{ resource.service.name = "node-backend" && name = "backend.deliver" && status = error }
```

```traceql
{ resource.service.name = "node-backend" && name = "backend.deliver" && duration > 100ms }
```

```promql
histogram_quantile(0.99, sum by (le, channel) (rate(notification_delivery_duration_seconds_bucket{service_name="node-backend"}[5m])))
sum by (channel) (rate(notification_delivery_attempts_total{service_name="node-backend", success="False"}[5m]))
```

### 2.2 GetStatus

| Span | Attributes |
|---|---|
| `backend.get-status` | `notification.id` |

```traceql
{ resource.service.name = "node-backend" && name = "backend.get-status" }
```

### 2.3 RenderTemplate

| Span | Attributes |
|---|---|
| `backend.render-template` | `template.id` |

```traceql
{ resource.service.name = "node-backend" && name = "backend.render-template" && span.template.id = "welcome" }
```

---

## 3. node-process (background workers)

Each worker cycle is a new trace (`startRootSpan`). Outbound `fetch` calls
carry `traceparent` (injected by the helper's undici instrumentation, never by
hand); calls to `/health` are not traced and not counted (client-side health
exclusion).

| Worker | Interval | Root span | Children / attributes | Metrics |
|---|---|---|---|---|
| queue consumer | 30 s | `process.queue-consume` (`queue.batch_size`) | `process.dispatch-notification` (`notification.recipient`, `notification.channel`; ERROR on HTTP ≠ 200 or a fetch failure) → `POST` client span → `POST /notify` | `notifications.queue_depth` |
| retry worker | 60 s | `process.retry-cycle` (`retry.failed_count`) | `process.retry-attempt` (`retry.attempt`) | `notifications.retries_total` |
| cleanup worker | 3 min | `process.cleanup` (`cleanup.removed`) | — | `notifications.cleaned_total` |
| health checker | 60 s | `process.health-check` (`health.status_code`; ERROR when ≠ 200) | none (the `/health` call is excluded) | — |

Each worker runs its first cycle at start, then waits its interval. On
SIGTERM an aborted cycle records no ERROR span and no ERROR log.

Log records: `Starting notification workers`, `Notification workers stopped`,
`Retry cycle complete` (`retried`), `Cleaned expired notifications` (`count`),
`Failed to dispatch` / `Health check failed` (`error`), `Worker cycle failed`
(`exception.type`, `exception.message`).

```traceql
{ resource.service.name = "node-process" && name = "process.queue-consume" && span.queue.batch_size > 3 }
```

```traceql
{ resource.service.name = "node-process" && name = "process.health-check" && status = error }
```

```promql
notifications_queue_depth{service_name="node-process"}
rate(notifications_retries_total{service_name="node-process"}[5m])
increase(notifications_cleaned_total{service_name="node-process"}[1h])
```

---

## Cross-service trace

```text
[node-process] process.queue-consume
└── [node-process] process.dispatch-notification
    └── [node-process] POST (fetch client)
        └── [node-api] POST /notify
            └── [node-api] request handler - /notify
                └── [node-api] api.send-notification
                    └── [node-api] notification.NotificationService/SendNotification (client)
                        └── [node-backend] notification.NotificationService/SendNotification (server)
                            └── [node-backend] backend.send-notification
                                ├── backend.render-template
                                └── backend.deliver
```

```traceql
{ resource.service.name = "node-api" && name = "api.send-notification" } >> { resource.service.name = "node-backend" && name = "backend.deliver" }
```

---

## Dashboard panels

```promql
# Throughput by channel
sum by (channel) (rate(notifications_sent_total{service_name="node-api"}[5m]))

# Error ratio
sum(rate(notifications_failed_total{service_name="node-api"}[5m]))
/
(sum(rate(notifications_sent_total{service_name="node-api"}[5m])) + sum(rate(notifications_failed_total{service_name="node-api"}[5m])))

# Delivery latency heatmap
sum by (le) (rate(notification_delivery_duration_seconds_bucket{service_name="node-backend"}[5m]))

# Queue saturation
notifications_queue_depth{service_name="node-process"}

# In-flight requests
notifications_active_requests{service_name="node-api"}
```

---

## Differences from Python and Go

The custom names and attributes match. What differs comes from the
instrumentations, the frameworks and the languages; a query or dashboard that
spans languages has to allow for each row.

### Custom signals

| Item | Node | Python | Go |
|---|---|---|---|
| `exception.type` on the span | `PaymentDeclinedError` (the error's `name`) | `__main__.PaymentDeclinedError` | `*main.PaymentDeclinedError` |
| `exception.type` on the `order failed` record | `PaymentDeclinedError` | `PaymentDeclinedError` (the logging handler uses the bare class name) | `*main.PaymentDeclinedError` |
| `exception.stacktrace` | on the span event and the record | on the span event and the record | neither (Go errors carry no stack) |
| `exception.escaped` on the span event | absent | `False` | absent |
| Log scope (instrumentation scope of the records; design D-JS14, 2026-11-03) | `node-custom-signals` (the `createLogger` name) | `custom_signals` (the `logging` logger name) | `otel-helper` (the helper's slog bridge) |
| Exemplars on the metric points | none (`@opentelemetry/sdk-metrics` 3.0 exports no exemplar filter: `OTEL_METRICS_EXEMPLAR_FILTER` is a recorded gap) | trace/span IDs of the recording span | trace/span IDs of the recording span |
| Integer counts (design D-JS14, 2026-11-03) | only with `valueType: ValueType.INT` (default: double) | from the `int` values added | `Int64*` instruments |

### Notification service

| Item | Node | Python |
|---|---|---|
| `http.route` template | `/notify/:notificationId` (express) | `/notify/{notification_id}` (FastAPI); Go `ServeMux` writes `/status/{id}` — a query on the route value is per language |
| Extra HTTP server spans | `request handler - <route>` (express) | `http receive` / `http send` (ASGI) |
| gRPC span names | `<service>/<method>` | the same (and Go), since contract task A30 |
| Unmatched route / malformed JSON | server span named `GET` / `POST`, no `http.route` | FastAPI returns 422 for malformed JSON (no 400) |
| 422 body | `{"detail": "<field>: …"}` (one string) | `{"detail": [{loc, msg, type, …}]}` (a list), also for malformed JSON |
| 404 body | `{"detail": "Not Found"}` | the same |
| 500 response | JSON `{"detail": "Internal Server Error"}`, the error logged through the helper (`Request failed`) | plain text `Internal Server Error`, the error logged by uvicorn, not through the helper |
| Log scopes | `node-api`, `node-backend`, `node-process` (the `createLogger` name) | the module name: `main` under uvicorn, `__main__` for `server.py` and `workers.py` |
| Node-only log records | `Notification API listening on :8000`, `Request failed`, `HTTP listen failed`, `gRPC bind failed`, `Worker cycle failed` | — (both: `Starting notification workers`, `Notification workers stopped`, `Backend gRPC server starting on :50051`) |
| Probe server of the workers (`:8080`) | `node:http`, instrumented: no span, counted in `http.server.request.duration` (no `http.route`) | aiohttp, not instrumented: nothing recorded |
| Runtime metrics | none | `system.*`, `process.*` |
| Backend past the caller's deadline | the handler finishes, its spans end, the reply is dropped | the same |
| Manual `traceparent` injection | never into an instrumented `fetch`: undici appends the header, so a second copy breaks the trace | python-process injects nothing either (httpx would replace the header) |

The legacy `rpc.*` gRPC attributes, the missing RPC metrics and the gRPC span
names are listed in the root README, "Known emitted-telemetry gaps".
