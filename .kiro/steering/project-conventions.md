# Project Conventions — staffops-otel-libs

## Core Principles

1. **OpenTelemetry as the single standard** — No vendor SDKs (Datadog, New Relic, etc.). Only `go.opentelemetry.io/*` and official OTel contrib packages.

2. **Everything via Collector** — SDK exports OTLP gRPC to the Collector. Never export directly to backends (Tempo, Loki, VictoriaMetrics).

3. **Sampling at the Collector** — SDK uses AlwaysOn (or configurable ratio). Tail sampling decisions happen at the Collector gateway, not in application code.

4. **Resource attributes at the Collector** — SDK sets only `service.name` and `deployment.environment`. The Collector's `k8sattributes` processor enriches with pod, namespace, node, and cloud metadata.

## Environment Variables Contract

All language implementations share the same env var interface:

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_NAME` | `my-service` | Service name |
| `ENVIRONMENT` | `LOCAL` | LOCAL, DEV, HML, PRD |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost` | Collector endpoint |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Conditional: SQL, AWS, REDIS |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0–1.0) |

## Cross-Language Consistency

- Same env vars across .NET, Python, Go.
- Same debug attribute (`debug=true` on root spans) for Collector tail-sampling.
- Same health path filtering (`/health`, `/healthz`, `/ping`, `/ready`).
- Same propagators (W3C TraceContext + Baggage).
- Same OTLP gRPC port (4317), insecure by default.

## Development Rules

- All builds and tests run via Docker (no local SDK dependency).
- Each language has: library code, unit tests, 3 example apps (api, backend, process).
- Examples demonstrate distributed tracing across HTTP → gRPC boundaries.
