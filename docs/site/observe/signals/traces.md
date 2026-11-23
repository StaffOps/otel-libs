# Traces

A trace shows one request across services: which calls it made, how long
each took, and which one failed. Use traces to answer "where" and "why
this request", after a metric has told you "how many" and "since when".

**Audience:** engineers investigating a request or an error.
**Backend:** Tempo, queried with TraceQL in Grafana Explore.

## What the helpers produce

| Span | Kind | Name | Key attributes |
|------|------|------|----------------|
| HTTP server | SERVER | `<METHOD> <route>` (`POST /orders`) | `http.request.method`, `http.route`, `http.response.status_code`, `error.type` on 5xx |
| HTTP client | CLIENT | `<METHOD>` | `server.address`, `url.full` (query values redacted), `http.response.status_code` |
| gRPC server and client | SERVER / CLIENT | `pkg.Service/Method` (`demo.v1.OrderService/PlaceOrder`) | `rpc.method`, the status code; `error.type` = the code name: CLIENT spans for every non-OK code, SERVER spans for the six server codes (`UNKNOWN`, `DEADLINE_EXCEEDED`, `UNIMPLEMENTED`, `INTERNAL`, `UNAVAILABLE`, `DATA_LOSS`) |
| Database | CLIENT | the operation and table, per driver | `db.system.name`, `db.namespace`, `db.response.status_code` and `error.type` (the SQLSTATE, `23505`) |
| Your own | INTERNAL (default) | the operation (`order.process`) | yours, and `error.type` on a failure |

Python and Node.js gRPC spans still carry the legacy `rpc.system`,
`rpc.service` and `rpc.grpc.status_code` keys. All the per-language
differences are in the [library overview](../../library/overview.md#known-emitted-telemetry-gaps).

### Failed spans carry the type, not the text

A failed span has status **ERROR without a description**, an `error.type`,
and at most an `exception` event that holds only `exception.type`. The
message and the stack trace are in a **correlated log record**
([ADR-014](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-014-exception-detail-in-correlated-logs.md)).
Errors from SQL drivers and HTTP/gRPC servers can quote request values, so
the text is kept out of spans, which are indexed, sampled and shared widely.
From a failed span, open **Logs for this span** ([Correlation](../correlation.md)).

The rule holds in Go, Python and Node.js for application spans (task A45),
SQL spans (A36) and HTTP/gRPC spans (A37). In .NET it holds for database and
AWS spans; ASP.NET Core and HttpClient spans still keep the text (parked .NET
tasks C20, C21). Redis and AWS spans in Go, Python and Node.js still keep the
server text (tasks A52, A53).

## Sampling: what reaches Tempo

1. The SDK samples with `parentbased_always_on`: every root is sampled, and
   a child follows its parent's sampled flag.
2. The Collector gateway tail-samples: it keeps errors, slow traces and a
   share of the rest, per environment.
3. Health checks get no server span.

So **Tempo does not hold every trace**: a request you see in a metric or a
log may have no trace. Counts come from span metrics, not from Tempo search
([Metrics](metrics.md)).

## TraceQL recipes

```text
# Failed server spans, with the error type
{ resource.service.name =~ "demo-.*-api" && kind = server && status = error }
  | select(span.error.type)

# Slow requests
{ resource.service.name = "demo-go-api" && kind = server && duration > 500ms }

# One error type across languages
{ span.error.type = "23505" }

# Failed database calls and what stayed on the span
{ kind = client && span.db.system.name != nil && status = error }
  | select(span.db.response.status_code, event.exception.type, statusMessage)

# Check: spans that still carry error text (expected empty, except .NET)
{ status = error && (statusMessage != "" || event.exception.message != nil) }
```

!!! note "Screenshot (phase 2)"
    Grafana Explore → Tempo, TraceQL
    `{ resource.service.name =~ "demo-.*-api" && status = error && kind = server } | select(span.error.type)`:
    the result table with one row per failed request and its `error.type`.

!!! note "Screenshot (phase 2)"
    The trace view of one demo `db_error` order: worker → api → backend → the
    PostgreSQL CLIENT span expanded, attributes `db.response.status_code=23505`,
    `error.type=23505`, no status message.

## Why traces are useful

- **Where the time went**: the waterfall shows the slow hop directly, which
  a p95 chart cannot.
- **Which dependency failed**: `error.type` on the failing CLIENT span,
  not just a 500 at the edge.
- **What a request looked like**: attributes (route, status, database) for
  one concrete request, which you then generalize with span metrics.
