# Project Conventions — staffops-otel-libs

> Last aligned with `0.1.0-rc.1` (2026-07-04). If behavior here conflicts with
> the code, treat it as a bug in one of the two and flag it — do not silently
> follow either side.

## Core Principles

1. **OpenTelemetry as the single standard** — No vendor SDKs (Datadog, New Relic, etc.). Only `go.opentelemetry.io/*` / `opentelemetry-*` / `OpenTelemetry.*` and official OTel contrib packages.

2. **Everything via Collector** — SDK exports OTLP gRPC to the Collector. Never export directly to backends (Tempo, Loki, VictoriaMetrics). **One exception:** when no OTLP endpoint is configured, metrics fall back to a Prometheus `/metrics` scrape endpoint on port 9464 (`OTEL_HELPER_METRICS_PORT`); traces stay in-process and logs go to stdout.

3. **Sampling at the Collector** — SDK uses AlwaysOn (or configurable ratio via `OTEL_HELPER_SAMPLE_RATIO`). Tail sampling decisions happen at the Collector gateway, not in application code.

4. **Resource attributes at the Collector** — SDK sets only `service.name` and `deployment.environment.name` (semconv ≥ v1.27 key; NOT the legacy `deployment.environment`). All three languages must emit both attributes identically — as of rc.1 only Go emits environment (with the legacy key); see `ANALISE-PROBLEMAS.md` P8. The Collector's `k8sattributes` processor enriches with pod, namespace, node, and cloud metadata.

5. **TLS by default** — `https://` or schemeless endpoints use TLS (system CA trust store). Plaintext requires explicit opt-in: `http://` scheme or `OTEL_EXPORTER_OTLP_INSECURE=true`. Explicit code config wins over env/scheme.

6. **Standard OTel env vars take precedence over `OTEL_HELPER_*` vars.** Resolution order everywhere: explicit code config > standard OTel env var (spec) > `OTEL_HELPER_*` env var > library default. Never invent a custom var when the OTel spec defines one (see `.kiro/specs/metrics-exporter-contract/`).

## Environment Variables Contract

All language implementations share the same env var interface:

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_NAME` | `my-service` | Service name (`OTEL_SERVICE_NAME` also honored) |
| `ENVIRONMENT` | `LOCAL` | LOCAL, DEV, HML, PRD |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(empty)_ | Collector endpoint; empty triggers the Prometheus metrics fallback. Custom ports MUST be preserved (default 4317 only when absent) |
| `OTEL_EXPORTER_OTLP_INSECURE` | _(unset)_ | `true` = plaintext, `false` = TLS. Unset = derived from scheme (secure by default) |
| `OTEL_METRICS_EXPORTER` | _(unset)_ | `otlp`, `prometheus`, `otlp,prometheus`, `none`. Unset = legacy behavior (OTLP if endpoint set, else Prometheus fallback). See metrics-exporter-contract spec |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Metric export interval in ms (standard OTel var; helper default is 30s, not the SDK's 60s) |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode (DEBUG logs, all instrumentations, `debug=true` on root spans) |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Conditional: SQL, AWS, REDIS |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0–1.0). Ignored if the standard `OTEL_TRACES_SAMPLER` is set |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(empty)_ | Comma list: traces, metrics, logs |
| `OTEL_HELPER_DISABLED_METRICS` | _(empty)_ | Wildcard patterns of metrics to drop (applies to ALL exporters — views are per-provider, not per-reader) |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Prometheus `/metrics` listener port |

## Cross-Language Consistency

- Same env vars and same resolution precedence across .NET, Python, Go.
- Same debug attribute (`debug=true` on root spans) for Collector tail-sampling.
- Same health path filtering (`/health`, `/healthz`, `/ping`, `/ready`).
- Same propagators (W3C TraceContext + Baggage).
- Same OTLP gRPC port default (4317), **TLS by default** (plaintext is opt-in).
- Same metric export interval (30s default).
- Any change to the env var contract or emitted attributes requires a spec in `.kiro/specs/` and must land in all three languages in the same release.

## Packaging

- **Core stays lightweight.** AWS/Redis/SQL instrumentations are opt-in subpackages: `OtelHelper.{AWS,Redis,Sql}` (NuGet), `otel-helper[aws,redis,sql]` (PyPI extras — the core package must NOT depend on these instrumentations), `ext/otel{aws,redis,sql}` (Go modules).
- **Libraries use version ranges, not exact pins.** Exact pins (`==`) belong in example/demo lockfiles only.

## Development Rules

- All builds and tests run via Docker (no local SDK dependency).
- Each language has: library code, unit tests (≥90% coverage CI gate), 3 example apps (api, backend, process).
- Examples demonstrate distributed tracing across HTTP → gRPC boundaries.
- Failures must be loud: never silently drop telemetry (log/return errors on exporter and listener startup failures).
