# Dashboards

Ready-made Grafana dashboards built on the demo's telemetry. Each one
answers one question, and together they cover RED, error handling, span
hygiene, database errors and exemplars. Use them as they are on the demo, or
as templates for a real service.

**Audience:** engineers who read dashboards, and dashboard authors.
**Source:** [`dashboards/demo/`](https://github.com/StaffOps/otel-libs/tree/main/dashboards/demo)
(Grafana JSON, folder `demo-otel-libs`).

## Import

1. Grafana → **Dashboards → New → Import** → upload the JSON file.
2. Map the data sources (UIDs `prometheus`, `loki`, `tempo`), or provision
   them with those UIDs ([wiring](../observe/overview.md#grafana-wiring)).
3. Use the **Language** and **Service** variables at the top to narrow the
   view.

The queries select the demo services by name
(`service_name=~"demo-(go|python|node|dotnet)-(api|backend|worker)"`). For a
real service, replace that matcher with your own `service_name`.

## The six dashboards

| # | File | Question | Main panels |
|---|------|----------|-------------|
| 0 | `00-start.json` | Start here: are the four answers right this hour? | database failures with a log (target 100 %); server spans still holding error text (target 0, except .NET); error logs without `trace_id` (target 0); duplicate copies of an error (target 0) |
| 1 | `01-overview.json` | Is the demo up? | requests per second, error percentage and p95 at the api; load per service; error ratio per language; worker responses by status; spans and logs per second per language; which duration metrics each service emits |
| 2 | `02-exc-logs.json` | Does every failure become a log linked to its trace? | error logs with a `trace_id`; error logs without one, and who writes them; duplicate copies from the web server; the same error counted per language |
| 3 | `03-span-hygiene.json` | Does any span still hold the error text? | spans with error text by kind (server, client, internal); conforming spans (type only); `error.type` per language |
| 4 | `04-db-errors.json` | Does every database failure produce a log with the detail? | coverage (records ÷ failed spans); failures × logs per language; SQLSTATE on the records; what stayed on recent failed spans |
| 5 | `05-exemplars.json` | From the chart to the right trace | database duration from the SDK metric (exemplar opens the parent span, task A43) versus from span metrics (exemplar opens the database span); latest error traces as a start for trace → logs; HTTP and gRPC histograms with exemplars |

!!! note "Screenshots (phase 2)"
    One full-page capture per dashboard (0 to 5), last hour, all languages.

## Reading them

- **Targets are in the panel descriptions.** Hover the `i` of a panel: it
  says what the panel measures and which value is good.
- **Compare languages, not absolute numbers.** The demo drives the same
  load and the same failure mix in every language: a bar that differs points
  at an instrumentation difference, which the
  [library overview](../library/overview.md#known-emitted-telemetry-gaps)
  usually lists.
- **Counters and restarts.** Panels that subtract a counter over the range
  (coverage) can exceed 100 % when a pod restarted inside the range: pick a
  range without a restart.

## A RED dashboard for your service

The three panels to start with, on span metrics (every language, every
request), with `service_name="<your-service>"`:

```promql
sum(rate(traces_span_metrics_calls_total{service_name="<your-service>", span_kind="SPAN_KIND_SERVER"}[$__rate_interval]))

sum(rate(traces_span_metrics_calls_total{service_name="<your-service>", span_kind="SPAN_KIND_SERVER", status_code="STATUS_CODE_ERROR"}[$__rate_interval]))
/ sum(rate(traces_span_metrics_calls_total{service_name="<your-service>", span_kind="SPAN_KIND_SERVER"}[$__rate_interval]))

histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_seconds_bucket{service_name="<your-service>", span_kind="SPAN_KIND_SERVER"}[$__rate_interval])))
```

Then add, by need: errors by `error_type`, the runtime saturation series of
your language, and a Loki panel with
`{service_name="<your-service>"} | severity_number >= 17`. See
[RED](../observe/diagnosis/red.md) and
[Latency and saturation](../observe/diagnosis/latency-saturation.md).

## History

Until this release the repository shipped three sample dashboards
(`01-api-business-metrics`, `02-workers-background`,
`03-traces-reliability`). They queried business metrics of the .NET example
only and span-metric names the Collector no longer produces. The demo
dashboards above replace them.
