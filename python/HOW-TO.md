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

Health checks (`/health /healthz /ready /readyz /live /livez /ping /metrics`, exact match,
case-insensitive, trailing slash and query ignored) produce no server span — and no child spans under
the default parent-based sampler — but **still count in the HTTP server metrics**, so failing probes stay
visible. Replace the list with `TelemetryOptions(health_paths=[...])` (`[]` traces every path).
`OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` / `OTEL_PYTHON_EXCLUDED_URLS` keep their upstream meaning (span
**and** metric dropped). Because probes now count in `http.server.request.duration`, exclude health
routes from SLI queries.

Outbound httpx/requests calls to the same list (including `health_paths`) are not traced and not
counted (upstream `excluded_urls`, anchored exact-path patterns merged with your
`OTEL_PYTHON_*_EXCLUDED_URLS`); gRPC clients skip the `grpc.health.v1.Health` service too. httpx reads
its exclusion list from the environment only, so the helper sets it for the duration of setup: clients
instrumented later with `HTTPXClientInstrumentor.instrument_client()` don't get the health exclusion.
Health paths may not contain commas.

If `from fastapi import FastAPI` runs **before** `setup_telemetry()`, the app is built from the
unpatched class: call `instrument_fastapi(app)` after creating it (idempotent).

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

The lib instruments `grpc.aio` and the sync `grpc.server()` / `grpc.insecure_channel()` / `grpc.secure_channel()` automatically via monkey-patch. **No need** to pass interceptors manually:

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

The whole `grpc.health.v1.Health` service (Check, Watch, List) is not traced, on servers and clients; spans your health servicer
starts itself become roots (there is no server span to parent them). `OTEL_PYTHON_GRPC_EXCLUDED_SERVICES`
works as upstream documents. Upstream limitation: the aio server interceptor only supports
server-streaming handlers written as async generators (`yield`); a traced handler using
`await context.write(...)` fails — health RPCs are never intercepted, exclude other such services
with `OTEL_PYTHON_GRPC_EXCLUDED_SERVICES`.

Context propagation (traceparent via gRPC metadata) is done automatically in both directions.

---

## 4. Logs

With a logs exporter active (OTLP endpoint, or `OTEL_LOGS_EXPORTER=console`/`otlp,console`), records
from standard `logging` — including uvicorn's non-propagating `uvicorn`/`uvicorn.access` loggers — are
exported with trace correlation and are **not** also printed to stdout. Without one (no endpoint,
`OTEL_LOGS_EXPORTER=none`) no OTel handler is attached: while the application configures no root
handler, records go to stdout at the environment level, and a later `logging.basicConfig()` takes over
normally. The helper sets the root level only when root has no handlers and is still at the stdlib
default `WARNING`. Code attributes (`code.*`) follow `OTEL_PYTHON_LOG_CODE_ATTRIBUTES` (default off).

- The stdout fallback writes one JSON object per line: `time`, `level` (`DEBUG/INFO/WARN/ERROR`),
  `msg`, `logger`, your `extra=` fields and `exception` (traceback) — the Go shape, plus `logger`
  (Python only). `time` is RFC 3339 with milliseconds (`Z` in UTC).
- Helper diagnostics (`otel_helper.*`, INFO+ — the helper sets that logger to INFO when you left it
  unset) and SDK diagnostics (`opentelemetry.*`, WARNING+) always reach stderr; SDK records are never
  exported (an export failure can't feed itself).
- `opentelemetry-instrument` / `LoggingInstrumentor().instrument()` add a second OTel handler; the
  helper removes it with a warning so each record is exported once. Set
  `OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION=false` to avoid it.
- The helper keeps its handler across `logging.basicConfig()` / `logging.config.dictConfig()` /
  `fileConfig()` by wrapping them during setup; a reference captured before setup
  (`from logging.config import dictConfig`) bypasses that and removes the OTel handler from root.

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
- Span attribute `debug=true` on entry spans (no parent or a remote parent; never from inbound tracestate/baggage) → Collector keeps 100% of these traces

The Collector uses the `debug-forced-attribute` policy (type: string_attribute, key: debug, values: ["true"]) to ensure debug traces are never dropped by tail sampling.

---

## 6. Sampling

### Default: `parentbased_always_on`

Root spans are always sampled; a span with a parent follows the parent's decision (a remote `traceparent` with `sampled=0` is not recorded). Tail-based sampling is done at the Collector. An unknown `OTEL_TRACES_SAMPLER` value logs a warning and uses this default.

### Head sampling (optional)

For high-volume scenarios where you want to reduce traffic before the Collector:

```bash
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.1  # 10% of root traces
# deprecated alias, same sampler: OTEL_HELPER_SAMPLE_RATIO=0.1
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
- gRPC (client + server, sync and async)
- System metrics (CPU, memory, GC, network)

> **Recommended**: Use the `otel_helper.ext` modules (see section 13) instead of `OTEL_HELPER_EXTRA_INSTRUMENTATION`. The env var still works for backward compatibility.

---

## Pre-fork servers (gunicorn `--preload`, uWSGI)

Call `setup_telemetry()` **only in each worker**, after the fork — never in the
preloaded module. Setup in the parent is inherited by every worker as-is —
on sdk 1.42–1.44 that includes one shared `service.instance.id` — and a second
call in a worker is a no-op, so it cannot be repaired there.

```python
# gunicorn.conf.py
def post_fork(server, worker):
    from otel_helper import setup_telemetry
    setup_telemetry()
```

uWSGI: decorate the same call with `@uwsgidecorators.postfork`. A worker
forked after setup already ran in the parent logs a warning on its next helper
call. Do not set `service.instance.id` yourself in multi-worker servers: every
worker would report the same id.

---

## Shutdown and SIGTERM

`shutdown_telemetry(timeout_ms=30000)` flushes and shuts down the trace, metric
and log providers (concurrently, under one deadline). The first call does the
work; later calls repeat its result — `None`, or the same `RuntimeError` — until
the next setup. It is also registered with `atexit`.

**Python skips `atexit` on an unhandled SIGTERM** (`docker stop`, a Kubernetes
rollout): buffered spans and logs (up to the batch delay) and the last metric
interval are lost. The helper installs no signal handler — the application owns
its signals. uvicorn and gunicorn handle SIGTERM and run the FastAPI lifespan
shutdown; call `shutdown_telemetry()` there:

```python
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from otel_helper import setup_telemetry, shutdown_telemetry

setup_telemetry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    try:
        shutdown_telemetry(timeout_ms=10_000)
    except RuntimeError as e:  # a provider failed or hit the deadline
        logging.getLogger(__name__).warning("telemetry shutdown incomplete: %s", e)


app = FastAPI(lifespan=lifespan)
```

A plain script or worker (no server) must turn SIGTERM into a normal exit itself,
as `example/python-process/workers.py` does:

```python
import signal, sys

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # runs finally/atexit
```

## 10. Validation

`setup_telemetry()` **fails with ValueError** when an option set in code is invalid. An invalid environment variable only logs a warning (logger `otel_helper`) naming the variable, and the default is used. Always call in `main()`:

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
| `OTEL_SERVICE_NAME` | Service name (`SERVICE_NAME` is a legacy alias it beats) | SDK `unknown_service*` + warning |
| `ENVIRONMENT` | Legacy alias for `deployment.environment.name` (`OTEL_RESOURCE_ATTRIBUTES` wins) | unset → attribute absent + warning |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint (per-signal `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` honored) | _(unset)_ |
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode | `false` |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Extra instrumentations | `SQL` |
| `OTEL_HELPER_SAMPLE_RATIO` | Sampling ratio (0.0-1.0). Ignored if the standard `OTEL_TRACES_SAMPLER` is set | `1.0` (`parentbased_always_on`) |
| `OTEL_HELPER_METRICS_PORT` | Standalone Prometheus `/metrics` listener port (`0` disables it) | `9464` |
| `OTEL_METRICS_EXPORTER` | Metric exporter(s): `otlp`, `prometheus`, `console`, a comma list, `none` | legacy inference |
| `OTEL_METRIC_EXPORT_INTERVAL` | OTLP metric export interval (ms) | `30000` |
| `OTEL_TRACES_SAMPLER` | Standard OTel sampler config — takes priority over `OTEL_HELPER_SAMPLE_RATIO` when set | _(unset)_ |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | OTLP wire protocol: `grpc` or `http/protobuf` | port-based inference |
| `OTEL_PROPAGATORS` | `tracecontext`, `baggage`, `none` (others warned and skipped; import `otel_helper` before other OpenTelemetry modules) | `tracecontext,baggage` |

**Precedence rule:** explicit code config (`TelemetryOptions(...)`) > standard OTel env var (`OTEL_METRICS_EXPORTER`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_TRACES_SAMPLER`) > `OTEL_HELPER_*` env var > library default.

---

## 12. FAQ

**Q: Do I need to pass interceptors for gRPC?**
A: No. The lib monkey-patches the `grpc.aio` and sync `grpc` channel/server constructors automatically. Just call `setup_telemetry()` before creating channels/servers.

**Q: Do I need to configure sampling?**
A: No. The SDK uses `parentbased_always_on` by default. Tail-based sampling is done at the Collector. Use `OTEL_HELPER_SAMPLE_RATIO` only in extreme volume scenarios.

**Q: Do I need to set resource attributes like `k8s.pod.name`?**
A: No. The Collector enriches automatically via `k8sattributes`.

**Q: How to correlate logs with traces?**
A: Automatic. The OTel handler adds `traceId`/`spanId` to all log records.

**Q: Can I call `setup_telemetry()` more than once?**
A: Yes, but only the first successful call takes effect: later calls return its resolved options and ignore their argument. If setup raises (e.g. the `/metrics` port is busy), nothing global is installed and a later call can retry. `shutdown_telemetry(timeout_ms=30000)` flushes and shuts down every provider; see [Shutdown and SIGTERM](#shutdown-and-sigterm). A setup after it re-enables logs and propagators only, because OpenTelemetry Python sets the global Tracer/MeterProvider once per process.

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

# Call the ext functions BEFORE setup_telemetry(): setup then finds them active and
# does not log the implicit-instrumentation deprecation warning (0.3).
instrument_aws()    # AWS SDK (boto3/botocore)
instrument_redis()  # Redis
instrument_sql()    # SQLAlchemy, all engines; or instrument_sql(engine=my_engine)

setup_telemetry()
```

| Module | Function | What it instruments |
|--------|----------|---------------------|
| `otel_helper.ext.aws` | `instrument_aws() -> bool` | boto3/botocore (S3, SQS, DynamoDB, etc.) |
| `otel_helper.ext.redis` | `instrument_redis() -> bool` | redis-py commands |
| `otel_helper.ext.sql` | `instrument_sql(engine=None) -> bool` | SQLAlchemy queries |

Each function returns `True` when the instrumentor is active and `False` when the
installed contrib refuses the installed library version. In that case one warning
on the `otel_helper` logger names the instrumentor, the installed version and the
supported range (the same warning is logged when `setup_telemetry()`
auto-instruments). Known case: SQLAlchemy 2.1 is not supported by
`opentelemetry-instrumentation-sqlalchemy` 0.63b0–0.66b1 (`< 2.1.0`); pin
`sqlalchemy<2.1`.

**Database attributes:** the helper defaults `OTEL_SEMCONV_STABILITY_OPT_IN` to
`http,database/dup`, so SQL spans carry both `db.system.name` (semconv v1.43) and
the legacy `db.system`, like Go's `ext/otelsql`. A value you set wins (`database`
→ new keys only; `http` alone → legacy keys only). Known gap: Python contrib copies
the SQLAlchemy dialect name into `db.system.name` unmapped, e.g. SQL Server →
`mssql`, where Go emits `microsoft.sql_server`; filter on both values in
cross-language queries.

> **Note**: `OTEL_HELPER_EXTRA_INSTRUMENTATION` is deprecated (removed in 1.0) and warns once when set; use the ext functions above.

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
| `otlp` | OTLP only. Without a metrics endpoint: warning and legacy inference (from code, `metric_exporters=["otlp"]` fails validation) |
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

`prometheus_metrics_port=0` (or `OTEL_HELPER_METRICS_PORT=0`) disables the standalone listener while keeping the Prometheus reader active for the mounted handler. If the port is already in use and not disabled, `setup_telemetry()` raises a `RuntimeError` naming `metrics_app()` as the fix. `metrics_app()` answers 503 before setup, after a failed setup and after `shutdown_telemetry()`; when Prometheus is off by configuration (`OTEL_SDK_DISABLED=true`, metrics disabled, or `OTEL_METRICS_EXPORTER` without `prometheus`) it answers 200 with an empty body, so the scrape target stays `up`.

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
| `host:4317` (no scheme) | TLS unless `OTEL_EXPORTER_OTLP_INSECURE=true` |

`OTEL_EXPORTER_OTLP_INSECURE` only decides for schemeless endpoints; an `http://`/`https://` scheme always wins and a contradicting value is warned. Examples:

```bash
# TLS endpoint (default behavior for https:// or schemeless)
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-gateway.example:4317

# Plaintext for a schemeless endpoint
OTEL_EXPORTER_OTLP_ENDPOINT=collector:4317
OTEL_EXPORTER_OTLP_INSECURE=true
```

Or in code:

```python
setup_telemetry(TelemetryOptions(insecure=True))
```

`insecure=` applies to a schemeless endpoint set in code; the scheme wins otherwise.

---

## 17. OTLP Protocol (gRPC vs HTTP/protobuf)

By default, the SDK exports over **gRPC**. To use OTLP/HTTP instead (e.g. behind an ingress that doesn't proxy gRPC), set the standard env var:

```bash
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

Or in code:

```python
setup_telemetry(TelemetryOptions(otlp_protocol="http/protobuf"))
```

If left unset, the protocol is inferred from the endpoint port: `4318` resolves to `http/protobuf`, any other port (including the default `4317`) resolves to `grpc`.

Priority: code config (`TelemetryOptions(otlp_protocol=...)`) > `OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL` > `OTEL_EXPORTER_OTLP_PROTOCOL` > port-based inference on that signal's endpoint (`4318` → `http/protobuf`) > `grpc` default.

IPv6 zone ids (`http://[fe80::1%25eth0]:4318`) work over gRPC only: the Python
OTLP/HTTP exporter (urllib3) cannot resolve them, and the helper logs a warning.

`http/json` is a valid value per the OTel spec but has no exporter implementation in this library: set in code it fails validation; from the env it is warned and ignored.

The `/v1/traces`, `/v1/metrics`, `/v1/logs` paths are appended to the endpoint automatically when `http/protobuf` is selected — do not include them in `OTEL_EXPORTER_OTLP_ENDPOINT`.