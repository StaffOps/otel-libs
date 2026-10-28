# OTel Helper — Python

OpenTelemetry instrumentation helper library for Python applications. A single call configures tracing, metrics, and logging following best practices.

## Installation

```bash
pip install otel-helper
```

Web/HTTP instrumentations (FastAPI, HTTPX, requests, gRPC, system-metrics) are installed automatically. SQLAlchemy, Redis, and botocore live in optional extras — install `otel-helper[sql]`, `[redis]`, `[aws]` (or `[all]`) as needed.

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
| `OTEL_SERVICE_NAME` | _(unset)_ | Service name. Unset everywhere → SDK default `unknown_service*` + a warning |
| `SERVICE_NAME` | _(unset)_ | Legacy alias, used only when neither `OTEL_SERVICE_NAME` nor `OTEL_RESOURCE_ATTRIBUTES` sets `service.name` |
| `ENVIRONMENT` | _(unset)_ | Legacy alias for `deployment.environment.name` (`OTEL_RESOURCE_ATTRIBUTES` wins). Unset → attribute absent + a warning; unknown values emitted verbatim |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | Collector endpoint. Per-signal `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` also honored (used verbatim) |
| `OTEL_EXPORTER_OTLP_INSECURE` | _(unset)_ | Applies to schemeless endpoints only (`true` = plaintext; any other value = false, non-`false` values warned); an `http://`/`https://` scheme always wins (a conflict is warned) |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode: DEBUG log, all instrumentations, attribute debug=true |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Conditional instrumentations: SQL, REDIS, AWS |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Deprecated: root-span ratio (0.0-1.0) as `parentbased_traceidratio`; prefer `OTEL_TRACES_SAMPLER`. 1.0 = `parentbased_always_on` |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Prometheus `/metrics` port when no OTLP endpoint is configured |

## Behavior by Environment

| Environment | Log Level |
|-------------|-----------|
| LOCAL | DEBUG |
| DEV/HML | INFO |
| PRD | WARNING |
| unset / other value | INFO |

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
| **Traces** | FastAPI requests, HTTPX/requests calls, gRPC client+server (sync and async), SQLAlchemy (if SQL), botocore (if AWS) |
| **Metrics** | System metrics (CPU, memory, GC, network), custom meters via OTLP, exemplars (trace-based). Exported every **30s**. |
| **Logs** | Python logging exported via OTLP with traceId/spanId automatically |

## Endpoint Resolution & TLS

Transport is determined by the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA trust store) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS; plaintext only with `OTEL_EXPORTER_OTLP_INSECURE=true` (or `TelemetryOptions(insecure=True)`) |

The scheme always wins over `OTEL_EXPORTER_OTLP_INSECURE` (OTLP spec); a contradicting value is logged as a warning. A gRPC URL without a port uses 4317 (with a warning); an HTTP URL without a port uses the scheme default (443/80).

For a local plaintext collector, use `http://` in the endpoint.

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

- SDK uses `parentbased_always_on` (default) — sampling is done at the Collector
- SDK only sets `service.name` — resource attributes are enriched by the Collector
- Everything exports via OTLP gRPC to the Collector, never directly to backends

## Documentation

📖 **[HOW-TO.md](HOW-TO.md)** — Practical guide (gRPC, workers, debug, sampling, metrics, logs)
