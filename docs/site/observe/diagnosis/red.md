# RED: rate, errors, duration

RED is the first look at any request-serving service. **Rate** is how many
requests it gets, **Errors** is how many fail, **Duration** is how long they
take. If all three are normal, the service is serving its users. If one
moves, it tells you where to look next.

**Audience:** engineers on call, dashboard authors.

## The three queries

Use span metrics: every language has them, they count every request before
sampling, and they carry `error_type`.

```promql
# Rate: server requests per second
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER"}[5m]))

# Errors: share of failed server spans
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[5m]))
/ sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER"}[5m]))

# Duration: p95 of server spans
histogram_quantile(0.95, sum by (le, service_name) (rate(traces_span_metrics_duration_seconds_bucket{span_kind="SPAN_KIND_SERVER"}[5m])))
```

The SDK histograms give the same view per protocol, with HTTP status codes
and exemplars:

```promql
# Error ratio from the SDK HTTP metric (5xx)
sum by (service_name) (rate(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[5m]))
/ sum by (service_name) (rate(http_server_request_duration_seconds_count[5m]))
```

Caveats per language:

- Python and Node.js have no gRPC server metric. .NET counts a gRPC call as
  HTTP status 200 even when it fails. Use span metrics for gRPC services.
- The SDK HTTP metrics include health checks: exclude their routes.

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 1. Overview**, the top row: requests per
    second at the api, error percentage at the api, p95 response time at the
    api, one value per language.

## Reading RED

| Pattern | Likely meaning | Next step |
|---------|----------------|-----------|
| Rate drops to zero | service down, or not exporting telemetry | check the pods, then the Collector |
| Errors up, rate flat | a dependency or a code path fails | break the errors down by `error_type` ([Errors](errors.md)) |
| Duration up, errors flat | saturation or a slow dependency | [Latency and saturation](latency-saturation.md) |
| One language differs from the others (demo) | instrumentation difference, not the app | [library overview](../../library/overview.md#known-emitted-telemetry-gaps) |

```promql
# Errors broken down by type
sum by (service_name, error_type) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[5m]))
```

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 1. Overview**, panel "Is the error ratio
    the same in every language?", with the 20 % reference line.

## RED per dependency

The callers' view of the same three numbers, from the service graph:

```promql
# Request rate and failures per edge
sum by (client, server) (rate(traces_service_graph_request_total[5m]))
sum by (client, server) (rate(traces_service_graph_request_failed_total[5m]))

# Server-side p95 per edge
histogram_quantile(0.95, sum by (le, client, server) (rate(traces_service_graph_request_server_seconds_bucket[5m])))
```

## Beyond RED

- **USE** (utilization, saturation, errors) for resources: runtime and pool
  metrics, [Latency and saturation](latency-saturation.md).
- **Golden signals**: RED plus saturation.
- For SLOs, build the ratio from the same span-metrics series, over the SLO
  window.
