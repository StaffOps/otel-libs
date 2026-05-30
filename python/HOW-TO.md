# HOW-TO: Using otel-helper (Python)

Practical guide for Python developers.

---

## 1. FastAPI

```python
from fastapi import FastAPI
from otel_helper import setup_telemetry

setup_telemetry()

app = FastAPI()

@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    return {"id": order_id}
```

Health checks (`/health`, `/healthz`, `/ready`, `/ping`) are automatically filtered from traces — both inbound (FastAPI) and outbound (HTTPX/requests).

---

## 2. Workers / Background Tasks

For workers running in a loop, each iteration should be an independent trace. Use `start_root_span`:

```python
from otel_helper import setup_telemetry, get_tracer
from otel_helper.tracing import start_root_span

setup_telemetry()
tracer = get_tracer("my-worker")

while True:
    with start_root_span(tracer, "process-batch") as span:
        span.set_attribute("batch.size", 100)
        process_items()
    time.sleep(60)
```

Without `start_root_span`, spans from different iterations become children of the same trace. With it, each cycle generates an independent trace (equivalent to .NET's `StartRootActivity`).

---

## 3. gRPC (Automatic)

The lib instruments `grpc.aio` automatically via monkey-patch. **No need** to pass interceptors manually:

```python
import grpc
from otel_helper import setup_telemetry

setup_telemetry()  # Instruments grpc.aio.insecure_channel and grpc.aio.server

# Client — automatic context propagation
async with grpc.aio.insecure_channel("backend:50051") as channel:
    stub = MyServiceStub(channel)
    response = await stub.MyMethod(request)

# Server — automatic context extraction
server = grpc.aio.server()
MyServiceServicer_to_server(MyServicer(), server)
```

Context propagation (traceparent via gRPC metadata) is done automatically in both directions.

---

## 4. Logs

Logs via standard `logging` are automatically exported with trace correlation:

```python
import logging
logger = logging.getLogger(__name__)

def process_order(order_id: str):
    logger.info("Processing order", extra={"order_id": order_id})
    # traceId and spanId are added automatically
```

### Log level by environment

| Environment | Level |
|-------------|-------|
| LOCAL | DEBUG |
| DEV/HML | INFO |
| PRD/BTC | WARNING |

`OTEL_HELPER_DEBUG_LEVEL=true` forces DEBUG in any environment.

### Internal OTel logs

In non-debug mode, `opentelemetry.*` logs are filtered to WARNING+ (avoids noise from deprecation warnings and export retries).

---

## 5. Debug Mode

When `OTEL_HELPER_DEBUG_LEVEL=true`:
- Log level: DEBUG
- Extra instrumentations: all active
- Span attribute `debug=true` on root spans → Collector keeps 100% of these traces

The Collector uses the `debug-forced-attribute` policy (type: string_attribute, key: debug, values: ["true"]) to ensure debug traces are never dropped by tail sampling.

---

## 6. Sampling

### Default: AlwaysOn

The SDK sends 100% of traces to the Collector. Tail-based sampling is done at the Collector.

### Head sampling (optional)

For high-volume scenarios where you want to reduce traffic before the Collector:

```bash
OTEL_HELPER_SAMPLE_RATIO=0.1  # 10% of traces
```

Or via code:
```python
setup_telemetry(TelemetryOptions(sample_ratio=0.5))
```

⚠️ **Caution**: head sampling may drop error traces before the Collector evaluates them. Use only when volume justifies it.

---

## 7. Custom Metrics

```python
from otel_helper import get_meter

meter = get_meter("my-service")
orders_counter = meter.create_counter("orders.received_total")
latency = meter.create_histogram("order.processing_duration_seconds")

def process_order():
    orders_counter.add(1, {"type": "standard"})
```

Exemplars (trace-based) are enabled automatically — metrics link to traces in Grafana.

---

## 8. Custom Traces

```python
from otel_helper import get_tracer

tracer = get_tracer("my-service")

with tracer.start_as_current_span("enrich-order") as span:
    span.set_attribute("order.id", order_id)
    do_work()
```

---

## 9. Conditional Instrumentations

Controlled via `OTEL_HELPER_EXTRA_INSTRUMENTATION`:

```bash
OTEL_HELPER_EXTRA_INSTRUMENTATION=SQL        # SQLAlchemy (default)
OTEL_HELPER_EXTRA_INSTRUMENTATION=SQL,REDIS   # SQLAlchemy + Redis
OTEL_HELPER_EXTRA_INSTRUMENTATION=SQL,AWS     # SQLAlchemy + boto3/botocore
```

| Value | Instrumentation |
|-------|-----------------|
| `SQL` | SQLAlchemy |
| `REDIS` | Redis |
| `AWS` | boto3/botocore (S3, SQS, DynamoDB, etc.) |

Instrumentations **always active** (no env var needed):
- FastAPI (server)
- HTTPX / requests (HTTP client)
- gRPC (client + server async)
- System metrics (CPU, memory, GC, network)

---

## 10. Validation

`setup_telemetry()` validates configuration and **fails with ValueError** if invalid. Always call in `main()`:

```python
# ✅ Correct — error visible at startup
def main():
    setup_telemetry()
    run_app()

# ❌ Wrong — error may be silently swallowed
setup_telemetry()  # at module top level, outside main
```

Error messages follow the pattern `[OtelHelper] ...` with indication of the required env var.

---

## 11. Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SERVICE_NAME` | Service name | `my-service` |
| `ENVIRONMENT` | Environment (LOCAL/DEV/HML/PRD/BTC) | `LOCAL` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint | `http://localhost` |
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode | `false` |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Extra instrumentations | `SQL` |
| `OTEL_HELPER_SAMPLE_RATIO` | Sampling ratio (0.0-1.0) | `1.0` (AlwaysOn) |

---

## 12. FAQ

**Q: Do I need to pass interceptors for gRPC?**
A: No. The lib monkey-patches `grpc.aio.insecure_channel` and `grpc.aio.server` automatically. Just call `setup_telemetry()` before creating channels/servers.

**Q: Do I need to configure sampling?**
A: No. The SDK uses AlwaysOn by default. Tail-based sampling is done at the Collector. Use `OTEL_HELPER_SAMPLE_RATIO` only in extreme volume scenarios.

**Q: Do I need to set resource attributes like `k8s.pod.name`?**
A: No. The Collector enriches automatically via `k8sattributes`.

**Q: How to correlate logs with traces?**
A: Automatic. The OTel handler adds `traceId`/`spanId` to all log records.

**Q: Can I call `setup_telemetry()` more than once?**
A: Yes, but only the first call takes effect. Subsequent calls are no-op.

**Q: Does Python 3.12+ work?**
A: Use Python 3.11. 3.12+ has issues with `pkg_resources` used by OTel instrumentations.
