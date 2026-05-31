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
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode: DEBUG log, all instrumentations, attribute debug=true |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Conditional instrumentations: SQL, REDIS, AWS |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0-1.0). 1.0 = AlwaysOn |

## Behavior by Environment

| Environment | Log Level |
|-------------|-----------|
| LOCAL | DEBUG |
| DEV/HML | INFO |
| PRD | WARNING |

`OTEL_HELPER_DEBUG_LEVEL=true` forces DEBUG log level in any environment.

## What is configured automatically

| Signal | What is captured |
|--------|-----------------|
| **Traces** | FastAPI requests, HTTPX/requests calls, gRPC client+server (async), SQLAlchemy (if SQL), botocore (if AWS) |
| **Metrics** | System metrics (CPU, memory, GC, network), custom meters via OTLP, exemplars (trace-based) |
| **Logs** | Python logging exported via OTLP with traceId/spanId automatically |

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
