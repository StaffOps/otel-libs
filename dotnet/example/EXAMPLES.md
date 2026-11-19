# EXAMPLES.md — Detailed Check and Simulation Descriptions

This document describes each check/simulation implemented in the samples, its objective, how it works internally, and how to query results in Grafana.

---

## SampleApi — Endpoints

### 1. `GET /order/{id}` — Full distributed trace

**Objective:** Show context propagation across 3 services over gRPC.

**Flow:**
1. API creates span `api.get-order` with tag `order.id`
2. Calls Backend over gRPC `ProcessOrder`
3. Backend creates spans `backend.db-query-order` (10-80ms) + `backend.external-enrich-order` (httpbin ~1s)
4. Response returns the order data

**Query in Grafana:**
```
# Tempo — full trace
{resource.service.name="sample-api" && name="api.get-order"}

# See the 3 services in the same trace
{resource.service.name=~"sample-api|sample-backend|sample-process" && name="api.get-order"}
```

---

### 2. `POST /order` — Creation with a JSON body

**Objective:** Show instrumentation of a POST endpoint with body parsing.

**Flow:**
1. Reads body `{"product": "...", "quantity": N}`
2. Generates a random ID
3. Calls Backend over gRPC `ProcessOrder`
4. Returns 201 with the data

**Query in Grafana:**
```
{resource.service.name="sample-api" && name="api.create-order"}
```

---

### 3. `GET /order/{id}/cancel` — Cancellation

**Objective:** Show a write operation (UPDATE) in the Backend.

**Flow:**
1. API creates span `api.cancel-order`
2. Calls Backend over gRPC `CancelOrder`
3. Backend creates span `backend.db-cancel-order` (5-30ms)

**Query in Grafana:**
```
{resource.service.name="sample-api" && name="api.cancel-order"}
```

---

### 4. `GET /slow` — Slow operation

**Objective:** Show high-latency detection by tail sampling (traces >1s always kept).

**Flow:**
1. API creates span `api.slow-operation`
2. Calls Backend over gRPC `SlowOperation`
3. Backend: `backend.db-heavy-query` (3-5s) + `backend.external-slow-enrichment` (1-2s)
4. Total: 4-7s

**Query in Grafana:**
```
# Slow traces
{resource.service.name="sample-api" && duration > 3s}

# The slow operation specifically
{resource.service.name="sample-api" && name="api.slow-operation"}
```

---

### 5. `GET /batch` — Sequential fan-out

**Objective:** Show a trace with many spans (fan-out pattern).

**Flow:**
1. API creates span `api.batch-orders`
2. Generates 5 random IDs
3. For each ID, creates span `api.batch-item` and calls Backend `ProcessOrder` sequentially
4. Total: ~37 spans in the trace (5 × backend spans)

**Query in Grafana:**
```
{resource.service.name="sample-api" && name="api.batch-orders"}
```

---

### 6. `GET /error` — Simulated exception

**Objective:** Show that error traces are always kept by sampling (100% in every environment).

**Flow:**
1. API creates span `api.error-simulated` with `StatusCode.Error`
2. Throws `InvalidOperationException`
3. Returns 500

**Query in Grafana:**
```
# Error traces
{resource.service.name="sample-api" && status=error}
```

---

### 7. `GET /health/ready` — Readiness probe

**Objective:** Show a health check that validates a dependency (Backend).

**Flow:**
1. API creates span `api.readiness-check`
2. Calls Backend over gRPC `ProcessOrder` with ID 0
3. Success → 200, failure → 503

**Query in Grafana:**
```
{resource.service.name="sample-api" && name="api.readiness-check"}
```

---

### 8. `GET /order/{id}/trace` — Baggage propagation

**Objective:** Show metadata (baggage) propagation across services without adding it to the span directly. Also shows scoped logging.

**How it works:**
1. API sets 3 baggage items: `tenant.id`, `order.id`, `feature.flag`
2. Uses `logger.BeginScope(...)` to add context to the log
3. Calls Backend over gRPC `ReadBaggage`
4. Backend reads `Baggage.Current` and returns the items it received
5. Baggage is propagated automatically through gRPC headers (no extra code)

**What it validates:**
- Baggage propagates across services automatically
- Backend receives the 3 items without extra configuration
- Scoped logging adds `TenantId` and `OrderId` to the logs

**Query in Grafana:**
```
# Tempo
{resource.service.name="sample-api" && name="api.trace-with-baggage"}

# Loki — see the baggage in the Backend logs
{service_name="sample-backend"} |= "ReadBaggage"
```

---

### 9. `GET /order/{id}/events` — Span Events

**Objective:** Show events inside a span (points in time, without creating sub-spans).

**How it works:**
1. API creates span `api.order-with-events`
2. Adds 4 events to the span:
   - `order.received` (with tags: order.id, order.source)
   - `order.validated` (after a validation delay)
   - `order.enriched` (after the Backend call)
   - `order.completed` (with tag: status)
3. Events appear as a timeline inside the span in Grafana

**What it validates:**
- Events are visible in the span detail in Tempo
- Each event has its own timestamp (shows the time between steps)
- Tags on events add context without cluttering the span

**Query in Grafana:**
```
# Tempo — open the span and see the "Events" tab
{resource.service.name="sample-api" && name="api.order-with-events"}
```

---

### 10. `GET /parallel/{count}` — Parallel fan-out

**Objective:** Show concurrent (parallel) spans inside a trace.

**How it works:**
1. API creates span `api.parallel-fan-out` with tag `fan_out.count`
2. Fires N calls to the Backend in parallel via `Task.WhenAll`
3. Each call creates span `api.parallel-item` (concurrent children)
4. In Grafana, the spans appear side by side (not sequential)

**What it validates:**
- Parallel spans are displayed correctly in Tempo
- Total duration ≈ duration of the slowest item (not the sum)
- Each item has its own span with tags

**Query in Grafana:**
```
{resource.service.name="sample-api" && name="api.parallel-fan-out"}
```

---

### 11. `GET /retry/{id}` — Retry with one span per attempt

**Objective:** Show a retry pattern where each attempt is a separate span, with ERROR on the failures and SUCCESS on the last one.

**How it works:**
1. API creates parent span `api.retry-operation`
2. Loop of up to 3 attempts, each with span `api.retry-attempt`
3. Calls Backend `UnstableOperation` (configured to fail 2x)
4. Attempts 1 and 2: span with `StatusCode.Error` + warning log
5. Attempt 3: success
6. Exponential backoff between attempts (100ms, 200ms)

**What it validates:**
- Each attempt is visible as a separate span in the trace
- Failed spans have status ERROR (red icon in Grafana)
- The parent span shows the whole operation
- Log with the exception stack trace correlated with the trace

**Query in Grafana:**
```
# Trace with retries
{resource.service.name="sample-api" && name="api.retry-operation"}

# Failures only
{resource.service.name="sample-api" && name="api.retry-attempt" && status=error}
```

---

### 12. `GET /cache/{id}` — Cache hit/miss

**Objective:** Show a cache pattern with hit-rate metrics and distinct spans.

**How it works:**
1. API creates span `api.cache-lookup` with tag `order.id`
2. Checks an in-memory cache (ConcurrentDictionary)
3. **HIT**: tag `cache.hit=true`, increments `cache.hits_total`, returns immediately
4. **MISS**: tag `cache.hit=false`, increments `cache.misses_total`, calls Backend, stores the result

**What it validates:**
- First call: MISS (long span, calls Backend)
- Second call with the same ID: HIT (short span, no Backend)
- Metrics `cache.hits_total` and `cache.misses_total` exported
- Tag `cache.hit` on the span allows filtering in Tempo

**Query in Grafana:**
```
# Tempo — filter by hit/miss
{resource.service.name="sample-api" && name="api.cache-lookup" && span.cache.hit=true}

# PromQL — cache hit rate
rate(cache_hits_total[5m]) / (rate(cache_hits_total[5m]) + rate(cache_misses_total[5m]))
```

---

## SampleBackend — RPCs

### `UnstableOperation`

**Objective:** Serve as the target of the API's `/retry` endpoint. Simulates an unstable service.

**How it works:**
- Keeps a global call counter
- Fails the first N calls (returns `StatusCode.Unavailable`)
- Succeeds every N+1 calls
- Span `backend.unstable-operation` with tag `attempt`

---

### `ReadBaggage`

**Objective:** Show that baggage propagated over gRPC is accessible in the downstream service.

**How it works:**
- Reads `Baggage.Current` (populated automatically by the OTel SDK from gRPC headers)
- Returns all items as a key→value map
- Adds each item as a tag on the span

---

## SampleProcess — Workers

The `dotnet-process` (and `dotnet-api`, `dotnet-backend`) metric names keep
the `_total` / `_seconds` suffixes (`queue.messages_processed_total`,
`scheduled_job.duration_seconds`, …) that the shipped Grafana dashboards query;
task C17 kept them for that compatibility. New code should follow
`dotnet-custom-signals`: dot-separated names, the unit in `unit`, no `_total`
(the Prometheus exporter adds it to counters).

### ApiHealthWorker (every 1 minute)

**Objective:** Generate distributed traces continuously, validating communication across the 3 services.

**How it works:**
- 6 sequential calls to the API, each with `StartRootActivity` (independent trace)
- Descriptive span names per endpoint:
  - `process-api-check` → `GET /`
  - `process-api-order` → `GET /order/1`
  - `process-api-cancel` → `GET /order/1/cancel`
  - `process-api-health-ready` → `GET /health/ready`
  - `process-api-batch` → `GET /batch`
  - `process-api-simulate-error` → `GET /error`

**Metrics:**
- `api_health.checks_total` (per endpoint)
- `api_health.checks_failed_total` (per endpoint)
- `api_health.check_duration_seconds` (per endpoint)

**Query in Grafana:**
```
# Tempo — all health checks
{resource.service.name="sample-process" && name=~"process-api-.*"}

# PromQL — success rate
1 - (rate(api_health_checks_failed_total[1h]) / rate(api_health_checks_total[1h]))
```

---

### HeavyProcessWorker (every 2 minutes)

**Objective:** Generate CPU and memory load to observe in the runtime metrics (.NET GC, thread pool).

**How it works:**
- Batch of 8-20 items in parallel (`StartRootActivity`)
- Each item: SHA256 ×2000 (50KB string) + 500 buffers (4-32KB)
- 5% chance of a simulated failure per item
- Span `process.heavy-batch` (root) → N× `process.heavy-process-item` (parallel children)

**Metrics:**
- `heavy_work.batches_total`
- `heavy_work.items_total` (tag: status)
- `heavy_work.errors_total`
- `heavy_work.batch_duration_seconds`
- `heavy_work.item_duration_seconds`
- `heavy_work.items_active` (gauge)

**Query in Grafana:**
```
# Tempo
{resource.service.name="sample-process" && name="process.heavy-batch"}

# PromQL — throughput
rate(heavy_work_items_total[5m])

# PromQL — error rate
rate(heavy_work_errors_total[5m]) / rate(heavy_work_items_total[5m])

# Runtime — GC pressure (correlate with batch execution)
dotnet_gc_collections_total
```

---

### QueueConsumerWorker (every 30 seconds)

**Objective:** Show the instrumentation pattern for queue consumers (SQS, Kafka, RabbitMQ).

**How it works:**
- Simulates a queue poll: generates 1-5 fake "messages" per cycle
- Each message is an independent trace (`StartRootActivity`)
- Inside the trace, 3 steps: parse → validate → persist
- 10% chance of a validation failure (span with ERROR)
- Observable gauge `queue.depth` shows the queue depth

**Span hierarchy per message:**
```
process.queue-consume (root)
  ├── process.queue-parse
  ├── process.queue-validate
  └── process.queue-persist
```

**Tags on the root span:**
- `messaging.message_id` — unique message ID
- `messaging.system` — "sqs"
- `messaging.destination` — "orders-queue"

**Metrics:**
- `queue.messages_processed_total` (tag: status)
- `queue.messages_failed_total`
- `queue.message_duration_seconds`
- `queue.depth` (observable gauge)

**Query in Grafana:**
```
# Tempo — message traces
{resource.service.name="sample-process" && name="process.queue-consume"}

# Tempo — validation failures only
{resource.service.name="sample-process" && name="process.queue-validate" && status=error}

# PromQL — message throughput
rate(queue_messages_processed_total[5m])

# PromQL — queue depth
queue_depth

# PromQL — failure rate
rate(queue_messages_failed_total[5m]) / rate(queue_messages_processed_total[5m])
```

---

### ScheduledJobWorker (every 3 minutes)

**Objective:** Show a scheduled job with a timeout, failure detection, and a circuit breaker.

**How it works:**
1. The job runs 3 steps: fetch-data → transform → persist
2. `fetch-data` has a random delay (100ms-7s) — it sometimes exceeds the 5s timeout
3. On timeout: span with ERROR + tag `timeout=true`
4. After 3 consecutive failures: the circuit breaker opens
5. Circuit open: the job is rejected (span with tag `circuit_breaker.action=rejected`)
6. After 30s: the circuit goes half-open and tries again
7. On success: the circuit closes and the counter resets

**Span hierarchy (success):**
```
process.scheduled-job (root)
  ├── process.job-fetch-data
  ├── process.job-transform
  └── process.job-persist
```

**Span hierarchy (timeout):**
```
process.scheduled-job (root, ERROR, timeout=true)
  └── process.job-fetch-data (cancelled by the timeout)
```

**Circuit breaker states:**
- `0` = closed (normal, jobs run)
- `1` = open (jobs rejected, waiting for recovery)
- `2` = half-open (trying to recover)

**Metrics:**
- `scheduled_job.runs_total` (tag: result = success/timeout/error/circuit-open)
- `scheduled_job.timeouts_total`
- `scheduled_job.duration_seconds`
- `circuit_breaker.state` (observable gauge: 0/1/2)

**Query in Grafana:**
```
# Tempo — all jobs
{resource.service.name="sample-process" && name="process.scheduled-job"}

# Tempo — timeouts only
{resource.service.name="sample-process" && name="process.scheduled-job" && span.timeout=true}

# PromQL — timeout rate
rate(scheduled_job_timeouts_total[1h]) / rate(scheduled_job_runs_total[1h])

# PromQL — circuit breaker state
circuit_breaker_state

# PromQL — job duration P95
histogram_quantile(0.95, rate(scheduled_job_duration_seconds_bucket[5m]))
```

---

## Summary of demonstrated features

| Feature | Where | Span/Metric |
|---|---|---|
| Distributed trace (3 services) | API `/order/{id}` | `api.get-order` |
| Baggage propagation | API `/order/{id}/trace` | `api.trace-with-baggage` |
| Span events | API `/order/{id}/events` | `api.order-with-events` |
| Parallel fan-out | API `/parallel/{count}` | `api.parallel-fan-out` |
| Retry with spans | API `/retry/{id}` | `api.retry-operation` |
| Cache hit/miss | API `/cache/{id}` | `api.cache-lookup` + `cache.hits_total` |
| Error trace (always kept) | API `/error` | `api.error-simulated` |
| High latency (always kept) | API `/slow` | `api.slow-operation` |
| gRPC instrumentation | API → Backend | Auto-spans gRPC client/server |
| StartRootActivity (workers) | All Process workers | Independent traces per iteration |
| Observable gauge | Process queue/circuit | `queue.depth`, `circuit_breaker.state` |
| Consumer pattern | Process queue | `process.queue-consume` |
| Timeout detection | Process job | `process.scheduled-job` + `timeout=true` |
| Circuit breaker | Process job | `circuit_breaker.state` gauge |
| CPU/memory stress | Process heavy | `process.heavy-batch` |
| Scoped logging | API `/order/{id}/trace` | `logger.BeginScope(...)` |
| Exceptions on spans | All (automatic for instrumented spans) | HTTP spans (ASP.NET Core, HttpClient): `exception` event with type, message and stack trace. Database and AWS spans: `exception.type` only; the message and stack trace go to an ERROR log record with the same `trace_id`/`span_id` (ADR-014). Custom spans: put the type on the span and log the detail inside the activity |
| Debug flag | Entry spans (when `OTEL_HELPER_DEBUG_LEVEL=true`) | Span attribute `debug=true` on entry spans (no parent or a remote parent); the helper no longer writes `debug=true` to the tracestate |

---

## Grafana dashboards

3 ready-made dashboards in [`dashboards/`](https://github.com/StaffOps/otel-libs/tree/main/dashboards) at the repository root. To import: **Dashboards → Import → Upload JSON**.

| File | Title | What it shows |
|---|---|---|
| `01-api-business-metrics.json` | Sample — API & Business Metrics | Request rate, error rate, P95 latency, cache hit/miss, revenue, DB/external duration, recent traces |
| `02-workers-background.json` | Sample — Workers & Background Processing | Health check success rate, heavy work throughput, queue depth, circuit breaker state, scheduled job timeouts |
| `03-traces-reliability.json` | Sample — Traces & Reliability Patterns | Retry attempts, parallel fan-out, baggage propagation, span events, timeout traces, queue consumer, all errors |

**Required datasources:**
- `victoriametrics` (type: prometheus) — metrics
- `tempo` (type: tempo) — traces

**Filter variable** (dashboard 01): `$service` — selects between `sample-api`, `sample-backend`, `sample-process`.

---

## Custom signals (`dotnet-custom-signals`)

Same names as the Go, Python and Node.js twins (`go-custom-signals`,
`python-custom-signals`, `node-custom-signals`): span, metric and log names,
attribute keys, units, instrument kinds and trace shape match. One deliberate
difference: the failed span carries the exception **type** only (ADR-014); the
message and the stack trace are on the `order failed` log record, which has the
same `trace_id`/`span_id`.

| Signal | Name | Kind | Attributes |
|---|---|---|---|
| span | `orders.process-batch` | root, Internal (`StartRootActivity`) | `batch.size`; status ERROR `1 of 3 orders failed` when an order fails |
| span | `order.process` | child, one per order (`ActivitySource.StartActivity`) | `order.id`, `order.type`; event `order.validated` (`order.amount`); on failure `error.type` `PaymentDeclinedException`, event `exception` with `exception.type` only, status ERROR `payment declined` |
| metric | `order.processed` (`{order}`) | Counter, `long` | `order.type`; `error.type` on failure only |
| metric | `order.duration` (`s`) | Histogram, `double`, buckets 5 ms – 1 s (`InstrumentAdvice<double>.HistogramBucketBoundaries`) | `order.type`; `error.type` on failure only |
| metric | `order.active` (`{order}`) | UpDownCounter, `long` | `order.type` |
| metric | `order.queue.depth` (`{order}`) | Observable gauge, `int` | `queue.name` |
| log | `order processed` (Information) | correlated with its `order.process` | `order.id`, `order.amount` |
| log | `order failed` (Error) | correlated with the failed `order.process` | `order.id`, `exception.type`, `exception.message`, `exception.stacktrace` |
| log | `batch finished` (Information) | correlated with `orders.process-batch` | `batch.size`, `orders.failed` |

Tracer and meter scope: `orders` (listed in `AdditionalActivitySources` and
`AdditionalMeters`: a source or meter named otherwise than `service.name` is
exported only when listed). Log scope: `dotnet-custom-signals` (the
`ILoggerFactory.CreateLogger` category).

How the code produces them
([`dotnet-custom-signals/Program.cs`](https://github.com/StaffOps/otel-libs/blob/main/dotnet/example/dotnet-custom-signals/Program.cs)):

| Signal | What the code does |
|---|---|
| Root span | `Source.StartRootActivity("orders.process-batch")` (a new trace whatever is current); `SetStatus(Error, "1 of 3 orders failed")` |
| Child spans | `Source.StartActivity("order.process")` per order; `AddEvent(new ActivityEvent("order.validated", tags: …))` |
| Failure | `SetStatus(Error, "payment declined")`, `SetTag("error.type", ex.GetType().Name)` (the short name, as the twins write `PaymentDeclinedError`; the process examples use `FullName`, see [README §4](README.md#4-errors-on-spans-adr-014)), `AddEvent(new ActivityEvent("exception", tags: { exception.type }))`. Not `Activity.AddException(ex)`: that adds `exception.message` and `exception.stacktrace` to the span |
| Counter | `Meter.CreateCounter<long>("order.processed", "{order}", …)` |
| Histogram | `Meter.CreateHistogram<double>("order.duration", "s", …, advice: new InstrumentAdvice<double> { HistogramBucketBoundaries = [0.005, …, 1] })` — measured: the Collector shows `ExplicitBounds #0: 0.005000` … `#7: 1.000000` |
| Up-down counter | `Meter.CreateUpDownCounter<long>("order.active", …)`, `+1` / `-1` around each order |
| Observable gauge | `Meter.CreateObservableGauge("order.queue.depth", () => new Measurement<int>(depth, queue.name=orders), …)` |
| Logs | `logger.Log(level, default, state, exception, (_, _) => "order processed")` with `state` a list of key/value pairs: the body is the fixed message, the pairs are the record's attributes on OTLP and fields on the stdout fallback (a logging scope would reach OTLP only). A message template (`"order processed {order.id}"`) would put the placeholder text into the body instead. The `Exception` argument becomes `exception.type` / `exception.message` / `exception.stacktrace` on OTLP and one `exception` field on the stdout fallback |

Ctrl-C / SIGTERM cancel `IHostApplicationLifetime.ApplicationStopping`, which
the loop checks before each order (`batch interrupted` Warning,
`orders.pending`); the flush still runs.

Recorded run (trace `ce6529746e6f6a4777d04632aa6ffb84`, 2026-11-12; IDs differ
on every run), condensed from the Collector's debug output:

```text
orders.process-batch  Internal  Status Error "1 of 3 orders failed"   batch.size=3
order.process         Internal  Status Unset   order.id=ord-1 order.type=standard  event order.validated
order.process         Internal  Status Unset   order.id=ord-2 order.type=express   event order.validated
order.process         Internal  Status Error   order.id=ord-3 error.type=PaymentDeclinedException
                                               events order.validated, exception (exception.type only)
LogRecord  Error  Body "order failed"  Span ID 8999edefbae5aa71 (the ord-3 span)
           exception.type PaymentDeclinedException, exception.message "card declined by issuer",
           exception.stacktrace "…at OrderJob.Charge(Order order) …", order.id ord-3
metrics (scope orders): order.processed, order.duration, order.active, order.queue.depth
```

The records carry only these attributes: no `{OriginalFormat}` (the state has
none), and no `SpanId`/`TraceId`/`ParentId` — the generic host's
activity-tracking scope is not exported (design D-NET10 (g), C17 fix round 1;
re-measured 2026-11-12, trace `c3aaa7bb80dcb94ce1eacb71ceb1ceaa`; the first
C17 run above still showed them). Data points of the synchronous instruments
carry exemplars with the span IDs.
