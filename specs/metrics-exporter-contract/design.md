# Design — Metrics Exporter Contract

## D1: No new subpackage

**Decision:** Implement in the core package of each language.

**Rationale:**
- Prometheus dependencies are already core dependencies in all three languages
  (`prometheus-client` in pyproject, `otel/exporters/prometheus` in go.mod,
  `OpenTelemetry.Exporter.Prometheus.HttpListener` in the csproj) — a
  subpackage would not slim anything.
- The Prometheus fallback already ships in core (rc.1); extracting it now
  would be a breaking change mid-release-candidate.
- The change is configuration of the existing MeterProvider, not new surface
  area: the OTel SDK supports multiple `MetricReader`s per provider in all
  three languages.

**Revisit if:** the .NET beta dependency (`Prometheus.HttpListener
1.9.0-beta.2`) blocks a stable `0.1.0` — then split `OtelHelper.Prometheus`
so core carries only stable deps. Not the default plan.

## D2: Multiple readers on one MeterProvider

Replace the either/or `if endpoint { otlp } else { prometheus }` with reader
accumulation. One provider, N readers — the SDK fans out every instrument to
all readers, so dual mode cannot double-count.

```
                       ┌── PeriodicExportingReader ──→ OTLP gRPC :4317 ──→ Collector
MeterProvider ── fan ──┤
                       └── PrometheusReader ──→ /metrics (listener OR mounted handler)
```

Resolution of the active reader set (per US-1):

```
exporters = explicit option
         ?? parse(OTEL_METRICS_EXPORTER)
         ?? (endpoint != "" ? ["otlp"] : ["prometheus"])   # legacy
"none" → empty set → no-op MeterProvider (reuse disabled-signal path)
"otlp" without endpoint → validation error at setup (fail-fast)
```

## D3: Env var precedence (US-2)

`explicit code > standard OTel env > OTEL_HELPER_* env > default`, applied in
each language's existing single resolution point (`resolve_from_env()` /
`resolveFromEnv()` / `TelemetryOptionsPostConfigure`) — no scattered
`os.getenv` at pipeline-build time, keeping options inspectable and testable.

Concretely in this spec:
- `export_interval_ms`: option > `OTEL_METRIC_EXPORT_INTERVAL` > 30000.
- Sampler: `OTEL_TRACES_SAMPLER` set → skip `OTEL_HELPER_SAMPLE_RATIO`
  entirely and let the SDK's own env handling configure the sampler.
- .NET note: today's hardcoded `ExportIntervalMilliseconds = 30_000` overrides
  the SDK's built-in reading of `OTEL_METRIC_EXPORT_INTERVAL`; the fix is to
  set it only from the resolved option.

## D4: Subset on /metrics = scraper-side filtering

SDK Views are per-provider, not per-reader — there is no supported way to
expose a subset on `/metrics` while pushing the full set via OTLP from one
provider. Options considered:

| Option | Verdict |
|---|---|
| Views per reader | Not supported by the SDK spec — rejected |
| Second parallel MeterProvider with curated instruments | Double footprint, duplicated instruments, non-global provider confusion — rejected |
| Expose all, filter at scrape (`metric_relabel_configs` / vmagent `drop`) | **Chosen** — market standard, zero SDK complexity |

Consequence: `OTEL_HELPER_DISABLED_METRICS` documents that it drops from ALL
exporters. HOW-TOs get a relabel recipe (US-5).

## D5: Listener vs mounted handler

Two consumption modes for the Prometheus reader output:

1. **Standalone listener** (default, current behavior): own HTTP server on
   `OTEL_HELPER_METRICS_PORT`. For workers/CLIs without an HTTP server.
   Single-process only.
2. **Mounted handler** (new): app mounts the scrape endpoint on its own
   server. For web apps, and the only correct mode under multi-worker
   (each worker answers the scrape of its own process).

Suppression: `OTEL_HELPER_METRICS_PORT=0` (or `with_metrics_listener(False)` /
`WithoutMetricsListener()` equivalents) disables the standalone listener while
keeping the Prometheus reader active for the mounted handler.

Per-language wiring:
- **Python:** `PrometheusMetricReader` registers into the default
  `prometheus_client` REGISTRY; `metrics_app()` wraps
  `prometheus_client.make_asgi_app()`. Listener failure (`OSError: port in
  use`) propagates from `setup_telemetry()` with a message pointing to
  `metrics_app()`.
- **Go:** create a dedicated `prometheus.NewRegistry()`, pass via
  `promexporter.WithRegisterer(registry)`; both the listener and
  `MetricsHandler()` serve `promhttp.HandlerFor(registry, ...)`. Listener gets
  `ReadHeaderTimeout: 5s`, error → `otel.Handle`, `srv.Shutdown` appended to
  the shutdowns slice in `otelhelper.go` (absorbs P5).
- **.NET:** keep `AddPrometheusHttpListener` for the listener mode; document
  `OpenTelemetry.Exporter.Prometheus.AspNetCore` +
  `MapPrometheusScrapingEndpoint()` as the web-app path (consumer adds that
  package; core does not take an ASP.NET Core dependency).

## D6: Backwards compatibility

- Unset `OTEL_METRICS_EXPORTER` reproduces rc.1 behavior bit-for-bit — no
  consumer action required on upgrade.
- `OTEL_HELPER_METRICS_PORT` semantics unchanged (except new `0` = disabled).
- New public API is additive only: `metrics_app()`, `MetricsHandler()`,
  `WithMetricExporters(...)` / `metric_exporters` / `MetricExporters`.

## Error handling summary

| Failure | Behavior |
|---|---|
| `otlp` requested, no endpoint | `validate()` error at setup (fail-fast) |
| Unknown exporter value | `validate()` error listing valid values |
| Listener port in use | Python: raise from setup. Go: `otel.Handle` + (preferred) synchronous `net.Listen` before goroutine so setup fails fast |
| Dual mode, collector down | OTLP reader retries per SDK; `/metrics` unaffected (independent readers) |
