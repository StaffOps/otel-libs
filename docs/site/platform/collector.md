# Collector prerequisites

What the Collector, the backends and the deployment charts must provide for
the helpers and for the observability pages of this site.

**Audience:** platform engineers who run the Collector and Grafana.

## Rollout checklist

The libraries assume a Collector and platform configured for them. These items
live outside this repo and block the 0.3.0 rollout; the full checklist, with
the reason for each item, is
[design D11](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d11--collector--platform-prerequisites-reference-checklist):

1. Tail sampling: a default branch for spans without
   `deployment.environment.name`, at the PRD rate.
2. The `debug=true` policy combined (`and`) with an environment allow-list and
   a `rate_limiting` sub-policy. Debug mode marks every entry span in every
   language — spans with no parent or a remote parent (the released .NET 0.2.x
   also prepended `debug=true` to the propagated tracestate; 0.3 no longer does)
   — so a debug-enabled service marks every inbound request.
3. Gateway `loadbalancingexporter` routed by trace ID; `decision_wait` ≥ the
   observed p99 trace duration, and the sampled-decision cache enabled for late
   spans.
4. Charts set `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` with
   `deployment.environment.name`; no `service.instance.id` for multi-worker
   containers.
5. Istio / mesh tracing at 100% or disabled (it interacts with the
   ParentBased sampler).
6. VictoriaMetrics OTLP ingest ignores volatile resource attributes
   (`-opentelemetry.ignoreResourceAttributes`, e.g. `process.pid`) and keeps
   `service.instance.id`.
7. A Collector `filterprocessor` dropping health spans, as defense in depth.
8. An explicit `:4317` on portless gRPC URLs.

## What the observability pages need

These items make the [Observability](../observe/overview.md) section work as
described (they are used on the k8s01 demo):

1. **Span-metrics connector** on the gateway, with the dimensions the
   queries use: `service.name`, `span.kind`, `status.code`, `span.name`,
   `error.type`, `db.system.name`, `db.response.status_code` (series `traces_span_metrics_*`, exemplars
   on).
2. **Service-graph connector** on the gateway (series
   `traces_service_graph_*`), with a database node when the client span has
   `db.namespace`.
3. **Exemplar storage** enabled on the metrics backend.
4. **Loki structured metadata** for OTLP logs: `trace_id`, `span_id` and the
   record attributes kept as metadata, queried with label filters.
5. The **Grafana data-source links**: exemplars to Tempo, Loki derived field
   to Tempo, Tempo traces-to-logs, traces-to-metrics and service map
   ([wiring](../observe/overview.md#grafana-wiring)).
