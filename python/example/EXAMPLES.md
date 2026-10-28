# Python examples — telemetry reference

What each example emits, span by span and metric by metric, with ready-to-use
queries (Tempo TraceQL, PromQL). Audience: developers building dashboards or
checking that their own instrumentation looks like the examples. How to run
the examples: [README.md](README.md).

Query conventions:

- Metric names assume the usual OTLP → Prometheus translation: dots become
  underscores, the unit is appended (`_seconds`), counters get `_total`
  (never twice: `notifications.sent_total` stays `notifications_sent_total`).
- `service_name` is the resource attribute `service.name` promoted to a label
  (VictoriaMetrics OTLP ingestion does this; with `target_info`-based backends
  join on it instead). On the Prometheus fallback (`:9464/metrics`) there is
  no `service_name` label: use the scrape job's labels.
- HTTP metrics use the stable semantic conventions (helper default
  `OTEL_SEMCONV_STABILITY_OPT_IN=http,database/dup`).

---

## Custom signals (`python-custom-signals`)

Same names as the Go twin (`go-custom-signals`).

| Signal | Name | Kind | Attributes |
|---|---|---|---|
| span | `orders.process-batch` | root, Internal | `batch.size`; status ERROR `1 of 3 orders failed` when an order fails |
| span | `order.process` | child, one per order | `order.id`, `order.type`; event `order.validated` (`order.amount`); on failure event `exception` (`__main__.PaymentDeclinedError`) + status ERROR `payment declined` |
| metric | `order.processed` (`{order}`) | Counter | `order.type`; `error.type` on failure only |
| metric | `order.duration` (`s`) | Histogram, buckets 5 ms – 1 s | `order.type`; `error.type` on failure only |
| metric | `order.active` (`{order}`) | UpDownCounter | `order.type` |
| metric | `order.queue.depth` (`{order}`) | Observable gauge | `queue.name` |
| log | `order processed` (INFO) | correlated with its `order.process` | `order.id`, `order.amount` |
| log | `order failed` (ERROR) | correlated with the failed `order.process` | `order.id`, `exception.type`, `exception.message`, `exception.stacktrace` |
| log | `batch finished` (INFO) | correlated with `orders.process-batch` | `batch.size`, `orders.failed` |

```traceql
{ resource.service.name = "python-custom-signals" && name = "order.process" && status = error }
```

```traceql
{ resource.service.name = "python-custom-signals" && name = "orders.process-batch" } >> { event:name = "exception" }
```

The PromQL names below are the helper's `/metrics` names; over OTLP they
depend on the backend's naming settings.

```promql
sum by (error_type) (rate(order_processed_total{service_name="python-custom-signals"}[5m]))
histogram_quantile(0.95, sum by (le) (rate(order_duration_seconds_bucket{service_name="python-custom-signals"}[5m])))
order_queue_depth{service_name="python-custom-signals", queue_name="orders"}
```

```logql
{service_name="python-custom-signals"} | trace_id = "<trace_id printed by the job>"
```

(The LogQL form depends on how the backend stores trace ids; in Loki's OTLP
ingestion they are structured metadata named `trace_id` / `span_id`.)

---

## Notification service — metrics

| Metric (instrument name) | Type | Attributes | Emitted by |
|---|---|---|---|
| `notifications.sent_total` | Counter | `channel` | python-api, per successful `POST /notify` |
| `notifications.failed_total` | Counter | `channel` | python-api, per `POST /notify` the backend reported failed |
| `templates.rendered_total` | Counter | — | python-api, per `POST /templates` |
| `notifications.active_requests` | UpDownCounter | — | python-api, in-flight `POST /notify` |
| `notification.delivery_duration_seconds` | Histogram (seconds, buckets 10 ms – 2.5 s) | `channel` | python-backend, per delivery |
| `notification.delivery_attempts_total` | Counter | `channel`, `success` | python-backend |
| `notifications.queue_depth` | UpDownCounter | — | python-process |
| `notifications.retries_total` | Counter | — | python-process |
| `notifications.cleaned_total` | Counter | — | python-process |
| `http.server.request.duration` | Histogram (s) | `http.request.method`, `http.route`, `http.response.status_code`, `url.scheme` | python-api (automatic, health probes included) |
| `http.client.request.duration` | Histogram (s) | `http.request.method`, `server.address`, `http.response.status_code` | python-process (automatic, httpx) |
| `system.*`, `process.*` | various | — | every app (automatic) |

The Python gRPC instrumentation emits **no RPC metrics**: backend latency comes
from `notification.delivery_duration_seconds`, from traces, or from the
caller's HTTP server metrics.

---

## 1. python-api (FastAPI :8000)

Every request gets an automatic server span named `<METHOD> <route>`
(`POST /notify`) plus ASGI `http receive` / `http send` internal spans, and is
counted in `http.server.request.duration`.

### 1.1 POST /notify

Sends a notification through the backend.

```text
POST /notify                              (server span)
└── api.send-notification
    ├── notifications.active_requests +1 / -1
    ├── gRPC client SendNotification → python-backend
    └── notifications.sent_total{channel} or notifications.failed_total{channel}
```

| Span | Attributes |
|---|---|
| `api.send-notification` | `notification.channel`, `notification.recipient`, `notification.template`; status ERROR `Backend reported failure` on failure |

Log record `Notification sent` (`notification_id`, `status`) is correlated with
`api.send-notification`.

```traceql
{ resource.service.name = "python-api" && name = "api.send-notification" && span.notification.channel = "email" }
```

```promql
notifications_active_requests{service_name="python-api"}
sum by (channel) (rate(notifications_sent_total{service_name="python-api"}[5m]))
histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="python-api", http_route="/notify", http_request_method="POST"}[5m])))
```

### 1.2 GET /notify/{notification_id}

Returns the status from the API's store, or asks the backend (`GetStatus`).

| Span | Attributes |
|---|---|
| `api.get-status` | `notification.id` |

```traceql
{ resource.service.name = "python-api" && name = "api.get-status" && span.notification.id = "866bbcb5" }
```

```promql
histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="python-api", http_route="/notify/{notification_id}", http_request_method="GET"}[5m])))
```

### 1.3 GET /templates and GET /metrics/summary

No custom span: the automatic server span only.

```traceql
{ resource.service.name = "python-api" && span.http.route = "/templates" && span.http.request.method = "GET" }
```

```promql
sum(rate(http_server_request_duration_seconds_count{service_name="python-api", http_route="/templates"}[5m]))
```

### 1.4 POST /templates

| Span | Attributes |
|---|---|
| `api.create-template` | `template.name` |

Increments `templates.rendered_total`.

```promql
rate(templates_rendered_total{service_name="python-api"}[5m])
```

### 1.5 GET /health

**No span** (health sampler), but **counted** in the HTTP server metrics, so
failing probes are visible:

```promql
sum(rate(http_server_request_duration_seconds_count{service_name="python-api", http_route="/health", http_response_status_code=~"5.."}[5m]))
```

Exclude probes from SLIs:

```promql
sum(rate(http_server_request_duration_seconds_count{service_name="python-api", http_route!~"/health|/healthz|/ready.*|/live.*|/ping|/metrics"}[5m]))
```

---

## 2. python-backend (gRPC :50051)

Every RPC gets an automatic server span named
`/notification.NotificationService/<Method>` (attributes `rpc.system`,
`rpc.service`, `rpc.method`, `rpc.grpc.status_code`), child of the caller's
client span. `grpc.health.v1.Health/*` calls are not traced.

### 2.1 SendNotification

```text
/notification.NotificationService/SendNotification   (server span)
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

```traceql
{ resource.service.name = "python-backend" && name = "backend.deliver" && status = error }
```

```traceql
{ resource.service.name = "python-backend" && name = "backend.deliver" && duration > 100ms }
```

```promql
histogram_quantile(0.99, sum by (le, channel) (rate(notification_delivery_duration_seconds_bucket{service_name="python-backend"}[5m])))
sum by (channel) (rate(notification_delivery_attempts_total{service_name="python-backend", success="False"}[5m]))
```

### 2.2 GetStatus

| Span | Attributes |
|---|---|
| `backend.get-status` | `notification.id` |

```traceql
{ resource.service.name = "python-backend" && name = "backend.get-status" }
```

### 2.3 RenderTemplate

| Span | Attributes |
|---|---|
| `backend.render-template` | `template.id` |

```traceql
{ resource.service.name = "python-backend" && name = "backend.render-template" && span.template.id = "welcome" }
```

---

## 3. python-process (background workers)

Each worker cycle is a new trace (`start_root_span`). Outbound httpx calls carry
`traceparent`; calls to `/health` are not traced and not counted (client-side
health exclusion).

| Worker | Interval | Root span | Children / attributes | Metrics |
|---|---|---|---|---|
| queue consumer | 30 s | `process.queue-consume` (`queue.batch_size`) | `process.dispatch-notification` (`notification.recipient`, `notification.channel`; ERROR on HTTP ≠ 200 or exception) → httpx client span → `POST /notify` | `notifications.queue_depth` |
| retry worker | 60 s | `process.retry-cycle` (`retry.failed_count`) | `process.retry-attempt` (`retry.attempt`) | `notifications.retries_total` |
| cleanup worker | 3 min | `process.cleanup` (`cleanup.removed`) | — | `notifications.cleaned_total` |
| health checker | 60 s | `process.health-check` (`health.status_code`; ERROR when ≠ 200) | none (the `/health` call is excluded) | — |

Log records: `Starting notification workers`, `Retry cycle complete`
(`retried`), `Cleaned expired notifications` (`count`), `Failed to dispatch`
/ `Health check failed` (`error`).

```traceql
{ resource.service.name = "python-process" && name = "process.queue-consume" && span.queue.batch_size > 3 }
```

```traceql
{ resource.service.name = "python-process" && name = "process.health-check" && status = error }
```

```promql
notifications_queue_depth{service_name="python-process"}
rate(notifications_retries_total{service_name="python-process"}[5m])
increase(notifications_cleaned_total{service_name="python-process"}[1h])
```

---

## Cross-service trace

```text
[python-process] process.queue-consume
└── [python-process] process.dispatch-notification
    └── [python-process] POST (httpx client)
        └── [python-api] POST /notify
            └── [python-api] api.send-notification
                └── [python-api] /notification.NotificationService/SendNotification (client)
                    └── [python-backend] /notification.NotificationService/SendNotification (server)
                        └── [python-backend] backend.send-notification
                            ├── backend.render-template
                            └── backend.deliver
```

```traceql
{ resource.service.name = "python-api" && name = "api.send-notification" } >> { resource.service.name = "python-backend" && name = "backend.deliver" }
```

---

## Dashboard panels

```promql
# Throughput by channel
sum by (channel) (rate(notifications_sent_total{service_name="python-api"}[5m]))

# Error ratio
sum(rate(notifications_failed_total{service_name="python-api"}[5m]))
/
(sum(rate(notifications_sent_total{service_name="python-api"}[5m])) + sum(rate(notifications_failed_total{service_name="python-api"}[5m])))

# Delivery latency heatmap
sum by (le) (rate(notification_delivery_duration_seconds_bucket{service_name="python-backend"}[5m]))

# Queue saturation
notifications_queue_depth{service_name="python-process"}

# In-flight requests
notifications_active_requests{service_name="python-api"}
```
