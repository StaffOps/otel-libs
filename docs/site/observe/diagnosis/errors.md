# Errors: from a spike to the stack trace

This page is a workflow for an error-rate increase. It ends with the error
message and the stack trace of a real failing request, in five steps that
use all three signals.

**Audience:** engineers on call.

## The workflow

1. **See it**: the error ratio rises on the RED panel ([RED](red.md)).
2. **Classify it**: break the failures down by `error_type` and span kind.

    ```promql
    sum by (service_name, span_kind, error_type) (
      rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}[5m]))
    ```

    A SERVER-only error type is the service's own failure. The same type on
    a CLIENT span of the same trace means a dependency failed.
3. **Find an example**: from an exemplar on the latency chart, or with
   TraceQL:

    ```text
    { resource.service.name = "demo-go-api" && status = error && span.error.type = "500" }
    ```

4. **Locate it**: in the trace view, the deepest failed span is usually
   the cause; the failed spans above it propagate the failure.
5. **Explain it**: on that span, **Logs for this span** opens the failure
   record, with the message, `exception.type` and `exception.stacktrace`.

If step 3 finds no trace, the failures were not sampled. Read the failure
records in Loki directly, filtered by `error_type` or `db_system_name`
([the unsampled dead link](../correlation.md#unsampled-records-and-the-dead-trace-link)).

## The demo scenarios

The demo fails about one order in ten, in three ways. Each one shows a
different error path:

| Scenario | Where it fails | What you see |
|----------|----------------|--------------|
| Duplicate order | the backend's PostgreSQL `INSERT` (`23505`) | database CLIENT span ERROR with `error.type=23505`; one database failure record; the gRPC call fails; the api answers `409` |
| Rejected order | the backend's business rule | a gRPC status code, the api answers `422` |
| Unhandled error | an exception in the api | the api SERVER span ERROR; `error.type` is `500` in Go and Python, the error type in Node.js express, and in .NET the exception's full type name (task A42, parked); one server failure record in Python and Node.js |

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 1. Overview**, panel "Which responses does
    the worker get from the api?": 201 versus 409, 422 and 500.

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 3. Span hygiene**, panel "Do error spans
    use the same error.type in every language?".

## Checks that keep error data useful

| Check | Query | Target |
|-------|-------|--------|
| Every error record links to a trace | `{service_name=~"demo-.*"} \| severity_number >= 17 \| trace_id = ""` | none; on the demo only Python `uvicorn.error` records remain (switch them off in the app) |
| No duplicate copies of the same error | records from `uvicorn.error` (Python, uncorrelated) or Kestrel (.NET, correlated) next to the helper's | none (switch them off in the app) |
| Spans keep no error text | TraceQL `{ status = error && (statusMessage != "" \|\| event.exception.message != nil) }` | none, except .NET (parked tasks C20, C21) |

!!! note "Screenshot (phase 2)"
    Dashboard **demo-otel-libs — 2. Exceptions and logs**, panel "Does the
    same error appear the same number of times in each language?".

## Pitfalls

- **4xx is not a server error by itself.** By semantic conventions an HTTP
  server span is ERROR for a 5xx, not for a 4xx; an application span around
  the handler can still be marked failed. To count business outcomes such as
  `409`, use `http_response_status_code` on the client or server metric.
- **One failure, several records.** A failure that crosses several helper
  spans writes one record per span: count by `span_id`, or count only the
  records of the outermost span.
- **The message is not a label.** Never group by message: group by
  `error_type`, then read the messages of one group.
