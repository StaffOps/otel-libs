# Metrics

Metrics answer "how many, how fast, how full" for every request, cheaply
and over long ranges. They are where an alert or a dashboard starts. Once a
metric shows a problem, its exemplars and labels lead you to a trace.

**Audience:** dashboard authors and engineers on call.
**Backend:** a Prometheus-compatible backend (Prometheus on the demo,
VictoriaMetrics in production), queried with PromQL / MetricsQL.

## Two sources of request metrics

| Source | Series | Counts | Use it for |
|--------|--------|--------|------------|
| **SDK metrics**, from the instrumentations | `http_server_request_duration_seconds`, `http_client_request_duration_seconds`, Go `rpc_*_call_duration_seconds`, `db_client_operation_duration_seconds`, runtime metrics | every request, including health checks | per-language detail, runtime saturation, exemplars |
| **Span metrics**, from the Collector gateway | `traces_span_metrics_calls_total`, `traces_span_metrics_duration_seconds` | every span **before tail sampling** (SDK-dropped spans are not counted; none with the default `parentbased_always_on`), every language alike | rates, error ratios and latency compared across languages, by `span_kind`, `status_code`, `error_type`, `db_system_name` |
| **Service graph**, from the Collector gateway | `traces_service_graph_request_total`, `_failed_total`, `_server_seconds`, `_client_seconds` | one edge per caller → callee pair | dependency map, per-edge error rate |

Span metrics fill the SDK gaps: Python and Node.js have no RPC metrics, and
.NET counts gRPC as HTTP with status 200. They are the same for every
language, so the demo dashboards use them for cross-language panels. The
full per-language list is in [Auto-instrumentation](../../library/auto-instrumentation.md).

## OTel names in PromQL

| OTel | Prometheus |
|------|------------|
| `http.server.request.duration` (histogram, unit `s`) | `http_server_request_duration_seconds_bucket` / `_sum` / `_count` |
| `order.processed` (counter, unit `{order}`) | `order_processed_total` |
| attribute `http.response.status_code` | label `http_response_status_code` |
| resource `service.name` | label `service_name` (and `job`) |

## Exemplars: from a point to a trace

When a histogram is recorded inside a **sampled** span, the SDK attaches an
exemplar to the point: the `trace_id` and `span_id` of that span. In
Grafana, turn on **Exemplars** in the query options. Each exemplar is a dot
on the chart, and its tooltip has **View trace**.

- Filter: `OTEL_METRICS_EXEMPLAR_FILTER` (`trace_based`, the default;
  `always_on`; `always_off`). Node.js emits no exemplars.
- Verified on the demo for the HTTP histograms: Go, Python and .NET
  points carry exemplars, Node.js none.
- Known issue (task A43, open): the Go and .NET
  `db.client.operation.duration` exemplars point at the **parent** span, not
  the database span. The span-metrics histogram
  (`traces_span_metrics_duration_seconds`, `db_system_name="postgresql"`)
  points at the database span itself.
- A dot whose trace was dropped by tail sampling opens an empty trace (see
  [Correlation](../correlation.md)).

!!! note "Screenshot (phase 2)"
    Grafana Explore → Prometheus, exemplars on:
    `histogram_quantile(0.95, sum by (le, service_name) (rate(http_server_request_duration_seconds_bucket{service_name=~"demo-.*"}[5m])))`,
    with one exemplar tooltip open showing **View trace**.

## Query recipes

```promql
# Request rate per service (span metrics, every language)
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER"}[5m]))

# Error ratio per service
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[5m]))
/
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER"}[5m]))

# p95 latency from the SDK histogram
histogram_quantile(0.95, sum by (le, service_name) (rate(http_server_request_duration_seconds_bucket[5m])))

# Responses by status code, as seen by the caller
sum by (http_response_status_code) (rate(http_client_request_duration_seconds_count[5m]))
```

- Rate first, then sum: `sum(rate(x[5m]))`, never `rate(sum(x))`.
- Health requests are in the SDK HTTP metrics: exclude them by route in SLI
  queries.
- Never use a high-cardinality attribute (user, order or request id) as a
  metric attribute: put it on the span or the log.

## Without a Collector

With no OTLP endpoint, the helper serves the metrics on a Prometheus
`/metrics` listener (port 9464, `OTEL_EXPORTER_PROMETHEUS_PORT`), so a
scraper can still collect them. See [Overview](../../index.md#environment-variables-03-contract).

## Why metrics are useful

- **Cheap over time**: a month of p95 per service costs a few series, where
  traces are sampled and short-lived.
- **Sampling-independent**: span metrics count every request, so error
  ratios are exact.
- **Alertable**: rates, ratios and quantiles are what SLOs and alerts are
  built on.
