# Logs

Logs carry what spans and metrics leave out on purpose: the error message,
the stack trace, and the free text of what the code was doing. Every record
written inside a span carries that span's `trace_id` and `span_id`, so a log
is one click away from its trace.

**Audience:** engineers reading an error.
**Backend:** Loki, queried with LogQL in Grafana Explore.

## What a record carries

OTLP logs land in Loki with:

| Where | Fields |
|-------|--------|
| Stream labels | `service_name` and the other resource labels Loki promotes |
| Structured metadata | `trace_id`, `span_id`, `severity_number`, `severity_text`, `scope_name`, and the record's attributes with dots replaced: `exception_type`, `exception_message`, `exception_stacktrace`, `error_type`, `db_system_name`, `db_response_status_code`, `http_*`, `rpc_*` |
| Line | the record's body (the message) |

Filter structured metadata with a label filter after the selector
(`| trace_id="…"`, `| exception_type != ""`), not with a line filter
(`|= "…"`), which only searches the body.

Without a logs exporter (no endpoint, `OTEL_LOGS_EXPORTER=none`), each
helper writes one JSON object per line on stdout, with `trace_id` and
`span_id` when the record is written in a span.

## The failure record

When a span fails, the helper writes **one ERROR record in the span's
context** that holds the detail the span does not
([ADR-014](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-014-exception-detail-in-correlated-logs.md)):
the body is the message, with the attributes `exception.type`,
`exception.message` and `exception.stacktrace`. Who writes one:

| Failure | Go | Python | Node.js | .NET |
|---------|----|--------|---------|------|
| Your span, through the failure helper (A45) | yes | yes | yes | parked |
| SQL client span (A36) | yes, with `db.system.name`, `db.response.status_code`, `error.type` | yes, same | yes, same | yes (task C14a), without `db.system.name` and the SQLSTATE on the record |
| HTTP / gRPC **server** span (A37) | no record (otelgrpc and otelhttp hand over no exception) | yes, with `error.type` and the `http.*` / `rpc.*` attributes | yes, same | not yet: the text stays on the span (parked tasks C20, C21) |
| HTTP / gRPC client span | none | none | none | none |

Records are written **for unsampled spans too**. Their `trace_id` is valid,
but Tempo may not have that trace: see
[the unsampled dead link](../correlation.md#unsampled-records-and-the-dead-trace-link).

A failure that crosses several helper spans gives one record per span.
Treat the log pipeline as sensitive: a database or server message can quote
request values.

### Duplicates from web servers

Some frameworks log the same unhandled error again, without a trace context:
Python `uvicorn.error` (and grpcio's `grpc._server` for a raising sync
handler) writes them **without** a trace context: on the demo they are the
only error records with no `trace_id`. .NET Kestrel writes a duplicate that
**is** correlated (it carries the span's ids), so it shows up next to the
helper's record under "Logs for this span". The HOW-TO of
each language shows how to switch them off in the application.

## LogQL recipes

```logql
# Every error record of the demo
{service_name=~"demo-.*"} | severity_number >= 17

# Failure records only (they carry the exception type)
{service_name=~"demo-.*"} | severity_number >= 17 | exception_type != ""

# Database failure records, by SQLSTATE
sum by (db_response_status_code) (count_over_time(
  {service_name=~"demo-.*"} | severity_number >= 17 | db_system_name != "" [5m]))

# Error records that cannot be linked to a trace (on the demo: only Python uvicorn.error)
{service_name=~"demo-.*"} | severity_number >= 17 | trace_id = ""

# All records of one trace
{service_name=~"demo-.*"} | trace_id = "<32 hex>"
```

!!! note "Screenshot (phase 2)"
    Grafana Explore → Loki:
    `{service_name=~"demo-.*"} | severity_number >= 17 | exception_type != ""`,
    one record expanded with its structured metadata and the **trace_id**
    derived-field link.

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 2. Exceptions and logs**, panel
    "Who writes the error logs?".

## Why logs are useful

- **The why of a failure**: the message and the stack, which spans omit.
- **The full set**: a failure record is written for unsampled spans too, so
  Loki has every failure, even when Tempo has only a sample.
- **Joinable**: `trace_id` and `span_id` link each record to its trace and
  its exact span.
