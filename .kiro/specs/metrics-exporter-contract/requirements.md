# Requirements — Metrics Exporter Contract (`OTEL_METRICS_EXPORTER`)

## Overview

Extend the metrics pipeline in all three languages (.NET, Python, Go) so that
OTLP push and the Prometheus `/metrics` scrape endpoint can run
**simultaneously**, controlled by the standard OTel env var
`OTEL_METRICS_EXPORTER`. Also formalizes the env var precedence rule (standard
OTel vars over `OTEL_HELPER_*` vars) and adds mountable `/metrics` handlers for
multi-worker deployments.

Covers items **P4, P6 and P7** of `ANALISE-PROBLEMAS.md`. Motivating scenario:
environments without an OpenTelemetry Collector (or migrating gradually) that
scrape metrics via Prometheus while traces/logs still go OTLP — today the
helper forces an either/or choice.

**Explicit non-goals:**
- No new subpackage (see design.md D1).
- No per-reader metric filtering — exposing a *subset* on `/metrics` while
  pushing everything via OTLP is filtered at the scraper, not the SDK (D4).

---

## User Stories & Acceptance Criteria

### US-1: Exporter selection via `OTEL_METRICS_EXPORTER`

**As an** operator,
**I want** to choose the metrics export mode with the standard OTel env var,
**So that** I can run OTLP push and Prometheus scrape together (or either one) without code changes.

**Acceptance Criteria:**

| `OTEL_METRICS_EXPORTER` | Behavior |
|---|---|
| _(unset)_ | **Legacy (unchanged):** OTLP if endpoint set, else Prometheus fallback |
| `otlp` | OTLP only. Startup validation error if no endpoint configured |
| `prometheus` | `/metrics` only, even when an OTLP endpoint IS set |
| `otlp,prometheus` | Both readers on the same MeterProvider |
| `none` | Metrics disabled (same effect as `OTEL_HELPER_DISABLED_SIGNALS=metrics`) |

- Value parsing: comma-separated, case-insensitive, whitespace-trimmed.
- Unknown value → startup validation error naming the var and the valid values
  (fail-fast, consistent with existing `validate()`).
- Equivalent programmatic option in each language, which wins over the env var:
  - Python: `TelemetryOptions(metric_exporters=["otlp", "prometheus"])`
  - Go: `otelhelper.WithMetricExporters("otlp", "prometheus")`
  - .NET: `options.MetricExporters = "otlp,prometheus"`
- In dual mode, the same instrument appears in both outputs with identical
  values (single provider, two readers — no double counting).
- Only affects **metrics**. Traces/logs pipelines are untouched by this var.

---

### US-2: Standard OTel env vars take precedence

**As an** operator who already knows OpenTelemetry,
**I want** the spec-defined env vars to work,
**So that** the helper doesn't surprise me with proprietary knobs shadowing the standard ones.

**Acceptance Criteria:**
- Precedence everywhere: **explicit code config > standard OTel env var >
  `OTEL_HELPER_*` env var > library default.**
- `OTEL_METRIC_EXPORT_INTERVAL` (ms) is honored; helper default remains 30000
  (replacing today's hardcoded 30s in `python/otel_helper/metrics.py:40`,
  `go/metrics.go:60`, `dotnet/OtelHelper/MetricsSetup.cs:29`).
- If `OTEL_TRACES_SAMPLER` is set, the helper does NOT apply
  `OTEL_HELPER_SAMPLE_RATIO` (the SDK's sampler config wins).
- `OTEL_HELPER_*` vars keep working when the standard var is absent
  (backwards compatible).
- Precedence table documented in root `README.md`.

---

### US-3: Mountable `/metrics` handler

**As a** developer running a web app,
**I want** to mount `/metrics` on my application's own HTTP server,
**So that** scraping works under multi-worker servers (gunicorn) and I don't manage a second port.

**Acceptance Criteria:**
- Python: `otel_helper.metrics_app()` returns an ASGI app —
  `app.mount("/metrics", metrics_app())` on FastAPI/Starlette.
- Go: `otelhelper.MetricsHandler() http.Handler` — mountable on the app's mux.
- .NET: document the ASP.NET Core path (`AddPrometheusExporter()` +
  `app.MapPrometheusScrapingEndpoint()`); `HttpListener` stays for non-ASP.NET
  processes.
- When the handler is mounted, the standalone listener on port 9464 must be
  suppressible (option/env — a handler user must not be forced to also bind
  9464).
- Standalone listener remains the default for worker/CLI processes with no
  HTTP server (current behavior preserved).

---

### US-4: Listener robustness and lifecycle

**As an** operator,
**I want** the `/metrics` listener to fail loudly and shut down cleanly,
**So that** a busy port doesn't mean silent metric loss.

**Acceptance Criteria:**
- Go: `ListenAndServe` error is reported via `otel.Handle` (not swallowed);
  server has `ReadHeaderTimeout`; server `Shutdown` joins the composite
  shutdown chain returned by `Setup`; exporter uses a dedicated
  `prometheus.Registry` (not the client_golang global registry).
  (These are P5 fixes but the listener code is rewritten here, so they land
  together.)
- Python: port-in-use raises at `setup_telemetry()` time with an actionable
  message (mentioning `metrics_app()` for multi-worker deployments).
- Multi-worker limitation documented in each HOW-TO (gunicorn → use mount or
  `PROMETHEUS_MULTIPROC_DIR`).

---

### US-5: Scrape-side filtering documented

**As an** operator who wants only *some* metrics scraped,
**I want** a documented recipe,
**So that** I don't expect (unsupported) per-reader SDK filtering.

**Acceptance Criteria:**
- HOW-TOs gain a section with a `metric_relabel_configs` (Prometheus) and
  vmagent equivalent example.
- Documents that `OTEL_HELPER_DISABLED_METRICS` applies to the whole provider
  (drops from OTLP **and** `/metrics` alike).

---

### US-6: Cross-language parity tests

**As a** maintainer,
**I want** the same behavioral tests in the three languages,
**So that** the contract can't drift (the way P3/P8 did).

**Acceptance Criteria:**
- Per language, tests assert: each `OTEL_METRICS_EXPORTER` value from US-1
  (including error cases), dual-mode single-provider (same counter visible in
  in-memory OTLP reader AND `/metrics` scrape), interval honored from
  `OTEL_METRIC_EXPORT_INTERVAL`, precedence order of US-2, handler mount
  serving Prometheus text format.
- Coverage stays ≥ 90% (CI gate).
