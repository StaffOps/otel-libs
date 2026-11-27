# Correlation

The three signals answer different questions: metrics say how much,
traces say where, logs say why. Correlation is the set of links that let you
follow one problem across all three without retyping ids or time ranges.

**Audience:** everyone using Grafana for diagnosis.
**Prerequisites:** the data sources wired as in the
[Observability overview](overview.md#grafana-wiring).

## The links

| From | To | How | What you need |
|------|----|-----|---------------|
| Metric point | Trace | **Exemplar** dot → **View trace** | histogram recorded in a sampled span; `exemplarTraceIdDestinations` on the metrics data source |
| Trace / span | Logs | **Logs for this span** in the trace view | `tracesToLogsV2` querying structured metadata `trace_id` / `span_id` |
| Log record | Trace | the **trace_id** derived field | `derivedFields` with `matcherType: label` (structured metadata) |
| Trace / span | Metrics | the span's **Related metrics** links | `tracesToMetrics` with the `service.name` → `service_name` tag |
| Service | Dependencies | Tempo **Service Graph** tab | the Collector service-graph connector; `serviceMap` on Tempo |

## Metric to trace: exemplars

Turn on **Exemplars** in a time-series or heatmap panel on a histogram, for
example `http_server_request_duration_seconds_bucket`. Each dot is one
recorded request. Open a dot in the slow band and choose **View trace**.

- SDK histograms carry exemplars in Go, Python and .NET (verified on the
  demo for HTTP); Node.js none.
- Span-metrics histograms (`traces_span_metrics_duration_seconds`) carry an
  exemplar per span, in every language.
- Go and .NET `db.client.operation.duration` exemplars open the **parent**
  span, not the database span (task A43, open). Use the span-metrics
  histogram with `db_system_name="postgresql"` for the database span itself.

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 5. Exemplars**, the two database heatmaps side
    by side: the SDK metric (the exemplar opens the parent span) and the
    span-derived metric (the exemplar opens the database span).

## Trace to logs

In the trace view, open a span and choose **Logs for this span**. Grafana
queries Loki for the records with that `trace_id` and `span_id`, ±5 minutes
around the span. For a failed span you get the failure record: message,
`exception.type` and the stack trace
([Logs](signals/logs.md#the-failure-record)).

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 5. Exemplars**, the table of most recent error
    traces → open a `db_error` trace → the failed database span →
    **Logs for this span**, showing exactly one ERROR record.

## Log to trace

In Explore → Loki, expand a record: the **trace_id** field links to the
trace in Tempo. This works only when the trace was kept, which leads to the
next section.

## Unsampled records and the dead trace link

!!! warning "A Loki record can link to a trace that Tempo does not have"
    The helpers write the failure record **for unsampled spans too**. The
    record carries a valid `trace_id`, but the trace was dropped by the SDK
    sampler or by tail sampling at the gateway, so it is not in Tempo, and
    the derived-field link opens an empty trace ("trace not found").

    It affects:

    - the **database failure records** (task A36) in Go, Python and Node.js;
    - the **HTTP and gRPC server failure records** (task A37) in Python and
      Node.js.

    **Do not rely on the link to count or group these failures.** Filter
    the records by their own attributes instead, which do not depend on
    sampling:

    ```logql
    # A36 database records
    {service_name=~"demo-.*"} | severity_number >= 17 | db_system_name != ""
    # A37 server records: select by origin (the helper's failure logger), then by error_type
    {service_name=~"demo-.*"} | severity_number >= 17
      | scope_name =~ "otel-helper|staffops_otel_helper.failure" | error_type != "" | db_system_name = ""
    ```

    The `error_type` of an A37 server record does not depend on sampling:
    the 5xx status code for an HTTP server span in Python (`500`), the
    error's type for Node.js express, and the gRPC code name (`UNKNOWN`,
    `INTERNAL`, …) for a gRPC server span. The `db_system_name = ""` filter leaves out the A36 database records, which the same logger writes. Group by it once the origin is
    selected.

    For the count of failed requests, use span metrics
    (`traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}`),
    which count every span before tail sampling (spans the SDK drops are not
    counted; there are none with the default `parentbased_always_on`).

This is the intended trade-off: Loki keeps every failure, while Tempo keeps
a sample. The same applies to exemplars: a dot whose trace was tail-sampled
away opens an empty trace. Pick another dot.

!!! note "Screenshot (phase 2)"
    A Loki record of an unsampled database failure, its **trace_id** link
    opened in Tempo with "trace not found".

## Trace to metrics

From a span, the **Related metrics** links (provisioned queries) open the
span's call rate by `status_code` and `error_type`, and its p95, filtered by
the span's service and name. Use them to see whether this one trace is
typical or an outlier.

## Service graph

Tempo's **Service Graph** tab draws the `traces_service_graph_*` edges: one
node per service and database, with request rate, error rate and latency
per edge. A database shows as a database node when the client span carries
`db.namespace` (Go task A38). On the demo, Redis shows as a virtual node
(its client spans name no database), and the Redis stream adds a messaging
edge from the api (producer) to the worker (consumer).

## Why correlation is useful

- **No copy-paste**: the ids and time range travel with the click.
- **The right detail at each step**: count in metrics, locate in traces,
  explain in logs.
- **Checks itself**: a link that opens nothing tells you about sampling,
  not about the bug.
