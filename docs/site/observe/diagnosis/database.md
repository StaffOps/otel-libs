# Database errors

A failed query is the most common hidden cause of an error at the edge. This
page shows what a database failure looks like in each signal, and how to
check that every failure kept its detail.

**Audience:** engineers on call, database owners.

## What a failure produces

| Signal | Content |
|--------|---------|
| Span (CLIENT) | status ERROR without a description; `db.system.name`, `db.namespace`, `db.response.status_code` and `error.type` = the SQLSTATE (`23505`) or the MySQL error number, else `_OTHER`; at most an `exception` event with `exception.type` only |
| Log | one ERROR record in the span's context: the message, `exception.type`, `exception.message`, `exception.stacktrace`, and `db.system.name`, `db.response.status_code`, `error.type` (task A36, Go, Python, Node.js). .NET writes the record without `db.system.name` and the SQLSTATE |
| Metrics | span metrics with `db_system_name` and `status_code`; the SDK `db_client_operation_duration_seconds` (Go, Node.js, .NET; not Python), Python `db_client_connections_usage` (pool), .NET `db_client_operation_failed_total` |

The record is written for unsampled spans too. Its trace link can be empty
([the unsampled dead link](../correlation.md#unsampled-records-and-the-dead-trace-link)),
so select database records by `db_system_name`, not through traces.

## Queries

```promql
# Failed database calls per second, per service
sum by (service_name) (rate(traces_span_metrics_calls_total{
  span_kind="SPAN_KIND_CLIENT", db_system_name="postgresql", status_code="STATUS_CODE_ERROR"}[5m]))
```

```logql
# The failure records, with the SQLSTATE
{service_name=~"demo-.*"} | severity_number >= 17 | db_system_name != ""

# Count by error code
sum by (db_response_status_code) (count_over_time(
  {service_name=~"demo-.*"} | severity_number >= 17 | db_system_name != "" [1h]))
```

```text
# TraceQL: what stayed on the failed spans
{ kind = client && span.db.system.name != nil && status = error }
  | select(span.db.response.status_code, span.error.type, event.exception.type, event.exception.message, statusMessage)
```

## Coverage check: does every failure have its record

The ratio "database failure records ÷ failed database spans" over the same
range should be 100 %:

- less means detail is being lost;
- more means duplicates, or a counter reset by a pod restart inside the
  range (pick a range without one).

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 4. Database errors**, panel "Failures ×
    logs, language by language": one row per language, failures and records
    equal, coverage 100 %.

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 4. Database errors**, panel "Do the database
    logs carry the error code (SQLSTATE)?" (`23505` in every language; .NET
    without a SQLSTATE).

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 4. Database errors**, panel "Recent failed
    database spans — what stayed on the span?".

## Language notes

- **Go**: the span-level `error.type` is the SQLSTATE; the duration
  metric's `error.type` stays the Go type name (`*pgconn.PgError`), and its
  exemplar points at the parent span (task A43, open).
- **Python**: SQLAlchemy statement spans have no `exception` event; the
  `connect` span keeps `exception.type` only. There is no operation-duration
  metric; the SQLAlchemy pool emits `db_client_connections_usage` (labels
  `pool_name`, which holds the connection URL, and `state`).
- **Node.js**: pg spans hand over no error object, so the record has no
  stack.
- **.NET**: the SQLSTATE is on the span; the record lacks `db.system.name`
  and the code (parked).

Per-language detail: the [library overview](../../library/overview.md#known-emitted-telemetry-gaps),
rows "Database error text" and "SQL and gRPC span `error.type`".
