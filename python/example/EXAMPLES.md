# Python Notification Service — Telemetry Examples

This document details the telemetry produced by each endpoint, gRPC method, and background worker in the Notification Service example. For each, you'll find the objective, internal span/metric flow, and ready-to-use Grafana queries (Tempo TraceQL + VictoriaMetrics PromQL).

---

## Architecture Overview

```
┌─────────────────────┐       gRPC :50051       ┌─────────────────────┐
│   python-api        │ ───────────────────────► │   python-backend    │
│   FastAPI :8000     │                          │   gRPC server       │
└─────────────────────┘                          └─────────────────────┘
                                                          ▲
                                                          │
                                                 ┌────────┴────────────┐
                                                 │   python-process    │
                                                 │   Background workers│
                                                 └─────────────────────┘
```

All three services export via OTLP gRPC to the OpenTelemetry Collector on `:4317`.

---

## Metrics Reference

| Metric | Type | Labels | Source |
|--------|------|--------|--------|
| `notifications.sent_total` | Counter | `channel` | backend |
| `notifications.failed_total` | Counter | `channel` | backend |
| `templates.rendered_total` | Counter | — | backend |
| `notifications.active_requests` | UpDownCounter | — | api |
| `notification.delivery_duration_seconds` | Histogram | — | backend |
| `notification.delivery_attempts_total` | Counter | — | backend |
| `process.queue_depth` | UpDownCounter | — | process |
| `process.items_processed_total` | Counter | — | process |

---

## 1. python-api (FastAPI :8000)

### 1.1 POST /notify

**Objective:** Accept a notification request, validate it, and forward to the backend for delivery.

**Internal Flow:**

```
HTTP POST /notify
  └─ api.send-notification
       ├─ validates payload
       ├─ increments notifications.active_requests (+1)
       ├─ calls backend.SendNotification (gRPC)
       └─ decrements notifications.active_requests (-1)
```

**Span:** `api.send-notification`

| Attribute | Example |
|-----------|---------|
| `notification.channel` | `email`, `sms`, `push` |
| `notification.recipient` | `user@example.com` |
| `notification.template` | `welcome_email` |

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-api" && name = "api.send-notification" }
```

Filter by channel:
```traceql
{ resource.service.name = "python-api" && name = "api.send-notification" && span.notification.channel = "email" }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
# Active requests right now
notifications_active_requests{service_name="python-api"}

# Request rate by channel (derived from traces)
rate(notifications_sent_total{service_name="python-backend"}[5m])
```

---

### 1.2 GET /notify/{id}

**Objective:** Retrieve the current status of a previously submitted notification.

**Internal Flow:**

```
HTTP GET /notify/{id}
  └─ api.get-status
       └─ calls backend.GetStatus (gRPC)
```

**Span:** `api.get-status`

| Attribute | Example |
|-----------|---------|
| `notification.id` | `notif-abc-123` |

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-api" && name = "api.get-status" }
```

Find a specific notification:
```traceql
{ resource.service.name = "python-api" && name = "api.get-status" && span.notification.id = "notif-abc-123" }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
# Latency p95 for status lookups (from HTTP server metrics)
histogram_quantile(0.95, rate(http_server_request_duration_seconds_bucket{service_name="python-api", http_route="/notify/{id}", http_request_method="GET"}[5m]))
```

---

### 1.3 GET /templates

**Objective:** List all available notification templates.

**Internal Flow:**

```
HTTP GET /templates
  └─ (auto-instrumented HTTP span)
       └─ reads template store
```

No custom span — relies on FastAPI auto-instrumentation.

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-api" && span.http.route = "/templates" && span.http.request.method = "GET" }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
rate(http_server_request_duration_seconds_count{service_name="python-api", http_route="/templates", http_request_method="GET"}[5m])
```

---

### 1.4 POST /templates

**Objective:** Create a new notification template.

**Internal Flow:**

```
HTTP POST /templates
  └─ api.create-template
       └─ persists template to store
```

**Span:** `api.create-template`

| Attribute | Example |
|-----------|---------|
| `template.name` | `password_reset` |

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-api" && name = "api.create-template" }
```

Filter by template name:
```traceql
{ resource.service.name = "python-api" && name = "api.create-template" && span.template.name = "password_reset" }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
rate(http_server_request_duration_seconds_count{service_name="python-api", http_route="/templates", http_request_method="POST"}[5m])
```

---

### 1.5 GET /metrics/summary

**Objective:** Return an aggregated summary of notification metrics (sent, failed, pending).

**Internal Flow:**

```
HTTP GET /metrics/summary
  └─ (auto-instrumented HTTP span)
       └─ reads internal counters
```

No custom span — relies on FastAPI auto-instrumentation.

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-api" && span.http.route = "/metrics/summary" }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
# Total sent across all channels
sum(notifications_sent_total{service_name="python-backend"})

# Total failed across all channels
sum(notifications_failed_total{service_name="python-backend"})
```

---

### 1.6 GET /health

**Objective:** Health check endpoint for liveness/readiness probes.

**Internal Flow:**

```
HTTP GET /health
  └─ (auto-instrumented HTTP span)
       └─ returns {"status": "healthy"}
```

No custom span — relies on FastAPI auto-instrumentation.

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-api" && span.http.route = "/health" }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
# Health endpoint error rate
rate(http_server_request_duration_seconds_count{service_name="python-api", http_route="/health", http_response_status_code=~"5.."}[5m])
```

---

## 2. python-backend (gRPC :50051)

### 2.1 SendNotification

**Objective:** Receive a notification request, render the template, and deliver via the appropriate channel (email/sms/push).

**Internal Flow:**

```
gRPC SendNotification
  └─ backend.send-notification
       ├─ backend.render-template
       │    └─ increments templates.rendered_total
       ├─ backend.deliver
       │    ├─ records notification.delivery_duration_seconds
       │    └─ increments notification.delivery_attempts_total
       ├─ increments notifications.sent_total{channel=...}  (on success)
       └─ increments notifications.failed_total{channel=...} (on failure)
```

**Span:** `backend.send-notification`

| Attribute | Example |
|-----------|---------|
| `notification.id` | `notif-abc-123` |
| `notification.channel` | `email` |
| `notification.recipient` | `user@example.com` |

**Child Span:** `backend.render-template`

| Attribute | Example |
|-----------|---------|
| `template.id` | `tmpl-welcome-001` |

**Child Span:** `backend.deliver`

| Attribute | Example |
|-----------|---------|
| `delivery.channel` | `email` |
| `delivery.duration_ms` | `245` |

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-backend" && name = "backend.send-notification" }
```

Find slow deliveries (> 500ms):
```traceql
{ resource.service.name = "python-backend" && name = "backend.deliver" && span.delivery.duration_ms > 500 }
```

Find failed notifications:
```traceql
{ resource.service.name = "python-backend" && name = "backend.send-notification" && status = error }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
# Sent rate by channel
rate(notifications_sent_total{service_name="python-backend"}[5m])

# Failed rate by channel
rate(notifications_failed_total{service_name="python-backend"}[5m])

# Delivery duration p99
histogram_quantile(0.99, rate(notification_delivery_duration_seconds_bucket{service_name="python-backend"}[5m]))

# Delivery duration p50
histogram_quantile(0.50, rate(notification_delivery_duration_seconds_bucket{service_name="python-backend"}[5m]))

# Delivery attempts rate
rate(notification_delivery_attempts_total{service_name="python-backend"}[5m])

# Templates rendered rate
rate(templates_rendered_total{service_name="python-backend"}[5m])
```

---

### 2.2 GetStatus

**Objective:** Look up the delivery status of a notification by ID.

**Internal Flow:**

```
gRPC GetStatus
  └─ (auto-instrumented gRPC span)
       └─ reads notification store
```

No custom span — relies on gRPC auto-instrumentation.

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-backend" && span.rpc.method = "GetStatus" }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
rate(rpc_server_duration_seconds_count{service_name="python-backend", rpc_method="GetStatus"}[5m])
```

---

### 2.3 RenderTemplate

**Objective:** Render a notification template with provided variables (called internally or directly via gRPC).

**Internal Flow:**

```
gRPC RenderTemplate
  └─ backend.render-template
       └─ increments templates.rendered_total
```

**Span:** `backend.render-template`

| Attribute | Example |
|-----------|---------|
| `template.id` | `tmpl-welcome-001` |

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-backend" && name = "backend.render-template" }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
rate(templates_rendered_total{service_name="python-backend"}[5m])
```

---

## 3. python-process (Background Workers)

### 3.1 QueueConsumer (every 30s)

**Objective:** Poll the notification queue and dispatch pending items to the backend for delivery.

**Internal Flow:**

```
Timer tick (30s)
  └─ process.queue-consume
       ├─ reads pending items from queue
       ├─ updates process.queue_depth
       ├─ for each item: calls backend.SendNotification (gRPC)
       └─ increments process.items_processed_total
```

**Span:** `process.queue-consume`

| Attribute | Example |
|-----------|---------|
| `queue.batch_size` | `12` |

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-process" && name = "process.queue-consume" }
```

Find large batches:
```traceql
{ resource.service.name = "python-process" && name = "process.queue-consume" && span.queue.batch_size > 50 }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
# Current queue depth
process_queue_depth{service_name="python-process"}

# Items processed rate
rate(process_items_processed_total{service_name="python-process"}[5m])

# Average batch size (from traces — requires Tempo metrics generator)
avg_over_time(traces_spanmetrics_latency_count{service_name="python-process", span_name="process.queue-consume"}[5m])
```

---

### 3.2 RetryWorker (every 60s)

**Objective:** Pick up failed notifications and retry delivery up to the configured maximum attempts.

**Internal Flow:**

```
Timer tick (60s)
  └─ process.retry-failed
       ├─ queries failed notifications
       ├─ for each: calls backend.SendNotification (gRPC)
       └─ increments process.items_processed_total
```

**Span:** `process.retry-failed`

| Attribute | Example |
|-----------|---------|
| `retry.count` | `5` |

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-process" && name = "process.retry-failed" }
```

Find high-retry batches:
```traceql
{ resource.service.name = "python-process" && name = "process.retry-failed" && span.retry.count > 10 }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
# Retry rate
rate(process_items_processed_total{service_name="python-process"}[5m])

# Correlate with failure rate
rate(notifications_failed_total{service_name="python-backend"}[5m])
```

---

### 3.3 CleanupWorker (every 3m)

**Objective:** Remove expired or completed notifications from the store to free resources.

**Internal Flow:**

```
Timer tick (3m)
  └─ process.cleanup-expired
       └─ deletes expired records
```

**Span:** `process.cleanup-expired`

| Attribute | Example |
|-----------|---------|
| `cleanup.removed_count` | `42` |

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-process" && name = "process.cleanup-expired" }
```

Find large cleanups:
```traceql
{ resource.service.name = "python-process" && name = "process.cleanup-expired" && span.cleanup.removed_count > 100 }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
# Queue depth should decrease after cleanup
process_queue_depth{service_name="python-process"}
```

---

### 3.4 HealthChecker (every 1m)

**Objective:** Periodically verify that the API and backend services are reachable and healthy.

**Internal Flow:**

```
Timer tick (1m)
  └─ process.health-check
       ├─ calls GET /health on python-api
       └─ records status
```

**Span:** `process.health-check`

| Attribute | Example |
|-----------|---------|
| `health.api_status` | `healthy`, `unhealthy` |

**Grafana — Tempo (TraceQL):**

```traceql
{ resource.service.name = "python-process" && name = "process.health-check" }
```

Find unhealthy checks:
```traceql
{ resource.service.name = "python-process" && name = "process.health-check" && span.health.api_status = "unhealthy" }
```

**Grafana — VictoriaMetrics (PromQL):**

```promql
# Health check span error rate (from Tempo metrics generator)
rate(traces_spanmetrics_calls_total{service_name="python-process", span_name="process.health-check", status_code="STATUS_CODE_ERROR"}[5m])
```

---

## Cross-Service Trace Example

A full notification flow produces a distributed trace spanning all three services:

```
[python-api] api.send-notification (POST /notify)
  └─ [python-backend] backend.send-notification (gRPC)
       ├─ [python-backend] backend.render-template
       └─ [python-backend] backend.deliver
```

When triggered via the queue:

```
[python-process] process.queue-consume
  └─ [python-backend] backend.send-notification (gRPC)
       ├─ [python-backend] backend.render-template
       └─ [python-backend] backend.deliver
```

**Grafana — Tempo (TraceQL) — Full trace:**

```traceql
{ resource.service.name = "python-api" && name = "api.send-notification" } >> { resource.service.name = "python-backend" && name = "backend.deliver" }
```

---

## Useful Dashboard Panels

### Notification Throughput (all channels)

```promql
sum by (channel) (rate(notifications_sent_total{service_name="python-backend"}[5m]))
```

### Error Ratio

```promql
sum(rate(notifications_failed_total{service_name="python-backend"}[5m]))
/
(sum(rate(notifications_sent_total{service_name="python-backend"}[5m])) + sum(rate(notifications_failed_total{service_name="python-backend"}[5m])))
```

### Delivery Latency Heatmap

```promql
rate(notification_delivery_duration_seconds_bucket{service_name="python-backend"}[5m])
```

### Queue Saturation

```promql
process_queue_depth{service_name="python-process"}
```

### Active In-Flight Requests

```promql
notifications_active_requests{service_name="python-api"}
```
