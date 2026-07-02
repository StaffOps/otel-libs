# OTel Helper — Python

OpenTelemetry instrumentation helper library for Python applications. A single call configures tracing, metrics, and logging following best practices.

## Installation

```bash
pip install otel-helper
```

All instrumentations (FastAPI, HTTPX, requests, gRPC, SQLAlchemy, Redis, botocore, system-metrics) are installed automatically. Activation of SQL/REDIS/AWS is controlled via env var.

## Quick Start

```python
from otel_helper import setup_telemetry

setup_telemetry()
```

With options:

```python
from otel_helper import setup_telemetry, TelemetryOptions

setup_telemetry(TelemetryOptions(
    service_name="checkout-api",
    resource_attributes={"app.component": "gateway"},
))
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_NAME` | `my-service` | Service name (priority over `OTEL_SERVICE_NAME`) |
| `OTEL_SERVICE_NAME` | `my-service` | Fallback for service name |
| `ENVIRONMENT` | `LOCAL` | Environment: LOCAL, DEV, HML, PRD |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost` | Collector endpoint |
| `OTEL_EXPORTER_OTLP_INSECURE` | _(unset)_ | TLS override: `true` = plaintext, `false` = TLS. Unset = derived from scheme (secure by default) |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode: DEBUG log, all instrumentations, attribute debug=true |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Conditional instrumentations: SQL, REDIS, AWS |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0-1.0). 1.0 = AlwaysOn |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Prometheus `/metrics` port when no OTLP endpoint is configured |

## Behavior by Environment

| Environment | Log Level |
|-------------|-----------|
| LOCAL | DEBUG |
| DEV/HML | INFO |
| PRD | WARNING |

`OTEL_HELPER_DEBUG_LEVEL=true` forces DEBUG log level in any environment.

## Behavior when no OTLP endpoint is configured

When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not set**, the library automatically falls back to:

| Signal | Behavior |
|--------|----------|
| Metrics | Exposed via Prometheus HTTP `/metrics` on port 9464 |
| Traces | In-process only (context propagation works, no export) |
| Logs | stdout/console only (no OTel export) |

The Prometheus metrics port is configurable via `OTEL_HELPER_METRICS_PORT` env var (default: 9464).

This enables the standard Kubernetes pattern: deploy without a collector, and let Prometheus/VictoriaMetrics scrape `/metrics` directly from the pod.

## What is configured automatically

| Signal | What is captured |
|--------|-----------------|
| **Traces** | FastAPI requests, HTTPX/requests calls, gRPC client+server (async), SQLAlchemy (if SQL), botocore (if AWS) |
| **Metrics** | System metrics (CPU, memory, GC, network), custom meters via OTLP, exemplars (trace-based). Exported every **30s**. |
| **Logs** | Python logging exported via OTLP with traceId/spanId automatically |

## Endpoint Resolution & TLS

Transport is determined by the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA trust store) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS (secure by default) |

Override with `OTEL_EXPORTER_OTLP_INSECURE`: `true` forces plaintext, `false` forces TLS. Explicit code config (`TelemetryOptions(insecure=True)`) always wins over env/scheme.

For a local plaintext collector, use `http://` in the endpoint or set `OTEL_EXPORTER_OTLP_INSECURE=true`.

## Opt-in Extensions

AWS, Redis, and SQL instrumentations are available as extras — not bundled in core:

```bash
pip install otel-helper[aws,redis,sql]
```

### Usage

```python
from otel_helper import setup_telemetry
from otel_helper.ext.aws import instrument_aws
from otel_helper.ext.redis import instrument_redis
from otel_helper.ext.sql import instrument_sql

setup_telemetry()

# Add only the instrumentations you need:
instrument_aws()
instrument_redis()
instrument_sql(engine=None)  # pass SQLAlchemy engine if available
```

| Extension | Install extra | Function |
|-----------|--------------|----------|
| AWS (botocore) | `otel-helper[aws]` | `instrument_aws()` |
| Redis | `otel-helper[redis]` | `instrument_redis()` |
| SQLAlchemy | `otel-helper[sql]` | `instrument_sql(engine=None)` |

> **Note**: `OTEL_HELPER_EXTRA_INSTRUMENTATION` env var still works for backward compatibility but explicit extensions are recommended.

## Architecture

```
[ Python App ]
      ↓ OTLP gRPC :4317
[ OTel Collector ]
      ↓
Tempo / VictoriaMetrics / Loki
```

- SDK uses AlwaysOnSampler (default) — sampling is done at the Collector
- SDK only sets `service.name` — resource attributes are enriched by the Collector
- Everything exports via OTLP gRPC to the Collector, never directly to backends

## Documentation

📖 **[HOW-TO.md](HOW-TO.md)** — Practical guide (gRPC, workers, debug, sampling, metrics, logs)
