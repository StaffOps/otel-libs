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
| PRD | WARNING |

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

| Value | Instrumentation | Requires extra |
|-------|-----------------|----------------|
| `SQL` | SQLAlchemy | `otel-helper[sql]` |
| `REDIS` | Redis | `otel-helper[redis]` |
| `AWS` | boto3/botocore (S3, SQS, DynamoDB, etc.) | `otel-helper[aws]` |

The env var only *activates* the instrumentation — the corresponding extra
must be installed. Without the extra, activation is silently skipped.

Instrumentations **always active** (no env var needed):
- FastAPI (server)
- HTTPX / requests (HTTP client)
- gRPC (client + server async)
- System metrics (CPU, memory, GC, network)

> **Recommended**: Use the `otel_helper.ext` modules (see section 13) instead of `OTEL_HELPER_EXTRA_INSTRUMENTATION`. The env var still works for backward compatibility.

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
| `ENVIRONMENT` | Environment (LOCAL/DEV/HML/PRD) | `LOCAL` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint | `http://localhost` |
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode | `false` |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Extra instrumentations | `SQL` |
| `OTEL_HELPER_SAMPLE_RATIO` | Sampling ratio (0.0-1.0). Ignored if the standard `OTEL_TRACES_SAMPLER` is set | `1.0` (AlwaysOn) |
| `OTEL_HELPER_METRICS_PORT` | Standalone Prometheus `/metrics` listener port (`0` disables it) | `9464` |
| `OTEL_METRICS_EXPORTER` | Metric exporter(s): `otlp`, `prometheus`, `otlp,prometheus`, `none` | legacy inference |
| `OTEL_METRIC_EXPORT_INTERVAL` | OTLP metric export interval (ms) | `30000` |
| `OTEL_TRACES_SAMPLER` | Standard OTel sampler config — takes priority over `OTEL_HELPER_SAMPLE_RATIO` when set | _(unset)_ |

**Precedence rule:** explicit code config (`TelemetryOptions(...)`) > standard OTel env var (`OTEL_METRICS_EXPORTER`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_TRACES_SAMPLER`) > `OTEL_HELPER_*` env var > library default.

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

---

## 13. Opt-in Extension Modules (`otel_helper.ext`)

AWS, Redis, and SQL instrumentations are available as **extras** — not bundled in the base install. Install only what you need:

```bash
pip install otel-helper[aws]
pip install otel-helper[redis]
pip install otel-helper[sql]
pip install otel-helper[aws,redis,sql]  # all at once
```

### Usage

```python
from otel_helper import setup_telemetry
from otel_helper.ext.aws import instrument_aws
from otel_helper.ext.redis import instrument_redis
from otel_helper.ext.sql import instrument_sql

setup_telemetry()

# AWS SDK (boto3/botocore) instrumentation
instrument_aws()

# Redis instrumentation
instrument_redis()

# SQL instrumentation (SQLAlchemy)
instrument_sql()  # auto-detects engine
# Or with explicit engine:
# instrument_sql(engine=my_engine)
```

| Module | Function | What it instruments |
|--------|----------|---------------------|
| `otel_helper.ext.aws` | `instrument_aws()` | boto3/botocore (S3, SQS, DynamoDB, etc.) |
| `otel_helper.ext.redis` | `instrument_redis()` | redis-py commands |
| `otel_helper.ext.sql` | `instrument_sql(engine=None)` | SQLAlchemy queries |

> **Note**: The `OTEL_HELPER_EXTRA_INSTRUMENTATION` env var still works for backward compatibility, but the ext modules are the recommended modern approach.

---

## 14. Metrics Without a Collector (Prometheus `/metrics`)

When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not set**, the library automatically falls back to local-only mode:

| Signal | Behavior |
|--------|----------|
| Metrics | Exposed via Prometheus HTTP `/metrics` on port 9464 |
| Traces | In-process only (context propagation works, no export) |
| Logs | stdout only (no OTel export) |

The port is configurable via `OTEL_HELPER_METRICS_PORT` env var (default: `9464`).

This enables the standard Kubernetes scrape pattern: deploy without a Collector, and let Prometheus/VictoriaMetrics scrape `/metrics` directly from the pod.

### Running OTLP push AND `/metrics` at the same time

`OTEL_METRICS_EXPORTER` selects which exporter(s) are active on the metrics pipeline, independent of the legacy endpoint-based fallback above:

| `OTEL_METRICS_EXPORTER` | Behavior |
|---|---|
| _(unset)_ | Legacy: OTLP if `OTEL_EXPORTER_OTLP_ENDPOINT` is set, else Prometheus fallback |
| `otlp` | OTLP only. Fails validation at startup if no endpoint is configured |
| `prometheus` | `/metrics` only, even when an OTLP endpoint IS set |
| `otlp,prometheus` | **Both** — one `MeterProvider`, two readers. Same instrument, same values, in both outputs |
| `none` | Metrics disabled entirely (equivalent to `OTEL_HELPER_DISABLED_SIGNALS=metrics`) |

The equivalent programmatic option — `TelemetryOptions.metric_exporters` — wins over the env var:

```python
setup_telemetry(TelemetryOptions(
    metric_exporters=["otlp", "prometheus"],
))
```

`OTEL_METRIC_EXPORT_INTERVAL` (milliseconds) controls the OTLP push interval; the library default is 30000ms, overriding the SDK's own 60s default. `TelemetryOptions.export_interval_ms` wins over the env var.

### Multi-worker deployments (gunicorn/uvicorn)

The standalone listener (`start_http_server`, port 9464 by default) opens one socket **per process** — under `gunicorn -w 4`, the second worker fails with `Address already in use`, and even if it didn't, each worker would expose only its own metrics on the same port.

Two ways to fix this:

1. **Mount `/metrics` on the app's own server** (recommended for FastAPI/Starlette):

```python
from otel_helper import setup_telemetry, metrics_app

app = FastAPI()
setup_telemetry(TelemetryOptions(
    metric_exporters=["prometheus"],  # or ["otlp", "prometheus"] for dual mode
    prometheus_metrics_port=0,         # disable the standalone listener
))
app.mount("/metrics", metrics_app())
```

2. **`prometheus_client`'s multiprocess mode** (`PROMETHEUS_MULTIPROC_DIR`) if you need the standalone listener behavior across workers — see the [prometheus_client docs](https://github.com/prometheus/client_python#multiprocess-mode-eg-gunicorn).

`prometheus_metrics_port=0` (or `OTEL_HELPER_METRICS_PORT=0`) disables the standalone listener while keeping the Prometheus reader active for the mounted handler. If the port is already in use and not disabled, `setup_telemetry()` raises a `RuntimeError` naming `metrics_app()` as the fix.

### Scoping down what gets scraped

`OTEL_HELPER_DISABLED_METRICS` drops instruments from **every** active exporter — there's no supported way to push everything via OTLP while exposing only a subset on `/metrics` from the same `MeterProvider` (the OTel SDK's Views are per-provider, not per-reader). If you need a narrower `/metrics` output while keeping the full set on OTLP, filter on the scrape side instead — e.g. Prometheus `metric_relabel_configs` or a vmagent `drop` relabel rule matching the instrument name.

---

## 15. Metrics Export Interval

Metrics are exported every **30 seconds** by default (not the SDK's 60s default) — set explicitly via `TelemetryOptions.export_interval_ms` or `OTEL_METRIC_EXPORT_INTERVAL` (ms). Applies to both OTLP export and the Prometheus `/metrics` fallback.

---

## 16. TLS Transport

OTLP export uses TLS by default. The transport is derived from the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA trust store) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS (secure by default) |

Override via env var:

```bash
# TLS endpoint (default behavior for https:// or schemeless)
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-gateway.example:4317

# Force plaintext for a local collector without TLS
OTEL_EXPORTER_OTLP_INSECURE=true
```

Or in code:

```python
setup_telemetry(TelemetryOptions(insecure=True))
```

Priority: code config (`TelemetryOptions(insecure=...)`) > `OTEL_EXPORTER_OTLP_INSECURE` env var > scheme-based detection.

For local development with a plaintext collector, the default `http://localhost` already resolves to plaintext — no changes needed.