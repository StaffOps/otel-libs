# Latency and saturation

When requests get slower but do not fail, the cause is either a slow
dependency or a saturated resource: CPU, memory, threads, a connection pool.
This page shows which series answer each case, per language.

**Audience:** engineers on call, performance work.

## 1. Which hop is slow

```promql
# p95 per service and span kind (span metrics)
histogram_quantile(0.95, sum by (le, service_name, span_kind) (
  rate(traces_span_metrics_duration_seconds_bucket[5m])))

# p95 per dependency edge (service graph, server side)
histogram_quantile(0.95, sum by (le, client, server) (
  rate(traces_service_graph_request_server_seconds_bucket[5m])))
```

Compare the CLIENT p95 of a caller with the SERVER p95 of its callee. A
large gap is time spent on the network or in a queue between them. Then open
a slow trace from an exemplar, or with
`{ resource.service.name = "demo-go-api" && kind = server && duration > 500ms }`.

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 1. Overview**, panel "How long do
    requests take? (p95)".

## 2. Is the process saturated

| Language | Look at | Saturation sign |
|----------|---------|-----------------|
| Go | `go_goroutine_count`, `go_memory_used_bytes`, `go_memory_gc_goal_bytes`, `go_processor_limit` | goroutines grow without bound; memory near the GC goal or `GOMEMLIMIT` |
| Python | `process_cpu_utilization_ratio`, `process_memory_usage_bytes`, `cpython_gc_collections_total`, `process_thread_count` | CPU near 1 per process (one core per interpreter); frequent GC |
| Node.js | none from the helper | use the container CPU and memory metrics of the platform |
| .NET | `dotnet_thread_pool_queue_length_total`, `dotnet_thread_pool_thread_count_total`, `dotnet_gc_pause_time_seconds_total`, `dotnet_monitor_lock_contentions_total`, `kestrel_queued_connections`, `kestrel_queued_requests` | the thread-pool queue grows; GC pause time rises; Kestrel queues connections |

Runtime metric names on other runtime versions are still to be measured
(phase 2), see [Auto-instrumentation](../../library/auto-instrumentation.md#still-to-be-measured-phase-2).

!!! note "Screenshot (phase 2)"
    Grafana Explore → Prometheus: `go_goroutine_count{service_name=~"demo-go-.*"}`
    and `rate(dotnet_gc_pause_time_seconds_total{service_name=~"demo-dotnet-.*"}[5m])`.

## 3. Is a connection pool exhausted

| Language | Series |
|----------|--------|
| Go | Redis pool: `db_client_connections_*` with `db_system="redis"` (`ext/otelredis`); SQL pool: `db_sql_connection_open`, `db_sql_connection_max_open`, `db_sql_connection_wait_total`, `db_sql_connection_wait_duration_milliseconds_total` (`ext/otelsql`) |
| Node.js | `db_client_connection_count`, `db_client_connection_max`, `db_client_connection_pending_requests` (`-sql`, pg) |
| .NET | `db_client_connection_count`, `db_client_connection_max`, `db_client_connection_npgsql_create_time_seconds` (Npgsql) |
| Python | `db_client_connections_usage` with `state` = `idle` / `used` (`[sql]`, SQLAlchemy pool; its `pool_name` label holds the connection URL, see [Auto-instrumentation](../../library/auto-instrumentation.md#metrics-emitted-automatically)) |

Pending requests above zero for a long time, or open connections at the
maximum, means the pool is the bottleneck: callers wait before the query
even starts, which shows as a gap before the database span in the trace.

## 4. Is it the code

When a span is slow and has no slow child, the time is in the code itself.
Add a child span around the suspect step, or, in .NET, use the profile of
that service ([Profiles](../signals/profiles.md)).
