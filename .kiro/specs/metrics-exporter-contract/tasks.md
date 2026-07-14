# Tasks — Metrics Exporter Contract Implementation Plan

## Status (2026-07-14)

| Phase | Status |
|-------|--------|
| 1. Options & contract resolution (3 langs) | ✅ Done (Python, Go, .NET) |
| 2. Metrics pipeline: multi-reader (3 langs) | ✅ Done (Python, Go, .NET) |
| 3. Mountable handlers + listener robustness | ✅ Done (Python, Go, .NET incl. ASP.NET Core HOW-TO) |
| 4. Standard env precedence (interval, sampler) | ✅ Done (Python, Go, .NET incl. dedicated .NET sampler tests) |
| 5. Docs (HOW-TOs, README, CHANGELOG, steering) | ✅ Done (all 3 language HOW-TOs, root README, root CHANGELOG; steering was already accurate) |

Prerequisite: PR 1 (`fix/python-packaging-endpoint`, P1–P3 of
`ANALISE-PROBLEMAS.md`) is **open, CI green, not yet merged**
(https://github.com/StaffOps/otel-libs/pull/1). This branch
(`feat/metrics-exporter-contract`) is stacked on top of it.

### Session notes (2026-07-14) — what's real vs. what's left

All code changes below are **in the working tree, uncommitted** — nothing on
this branch has been committed yet. Confirmed via `git status` twice in this
session (before and after re-verification below): same 18 modified + 6
untracked files both times, nothing staged.

**Re-verified from scratch in this session (not just repeated from memory):**
- **Python**: reran `pytest tests/ --ignore=tests/test_propagation.py
  --cov=otel_helper --cov-fail-under=90` → **132 passed, 95.38% coverage**,
  same result as the first run. Includes `test_metrics_contract.py` and
  `test_sampler_precedence.py`.
- **Go**: reran with `-count=1` (bypasses Go's test cache, so this is a real
  re-execution, not a cached "ok") → **all tests pass, 22.4s**. Isolated the
  22 new contract-test names explicitly (`Test_Exporters_*`, `Test_Interval_*`,
  `Test_DualMode_*`, `Test_Listener_*`, `Test_Sampler_*`,
  `Test_MetricsHandler_*`) with `-v` → all 22 individually confirmed PASS.
- **.NET**: this was genuinely unverified when first documented. `dotnet` CLI
  is not installed on this machine, so ran via
  `docker run --rm -v $(pwd)/dotnet:/src -w /src mcr.microsoft.com/dotnet/sdk:8.0
  dotnet test OtelHelper.Tests --configuration Release --no-restore`
  (same SDK version and command as `.github/workflows/ci.yml`'s `dotnet-test`
  job, minus the coverage flags — see caveat below). Result: **build
  succeeded, 159 passed, 0 failed, 0 skipped** in 322ms. This is a genuine
  fresh run (image pulled, project restored, code compiled) — the .NET
  contract code and tests are real and correct.

**Follow-up work completed after the first verification pass:**
- Fixed both `xUnit1031` warnings in `MetricsContractTests.cs` (converted
  `DualMode_CounterVisible_InInMemoryAndPrometheusReaders` and
  `ConfigureMetrics_PrometheusOnly_WithEndpoint_Builds` to `async Task` +
  `await` instead of `.GetAwaiter().GetResult()`). Verified via a clean
  `dotnet build` in Docker: **0 Warning(s), 0 Error(s)**.
- Added `dotnet/OtelHelper.Tests/SamplerPrecedenceTests.cs` (5 tests) closing
  the gap flagged below. Before writing it, empirically verified — via a
  disposable console app run in the same `mcr.microsoft.com/dotnet/sdk:8.0`
  container, not just by reading SDK source strings — that
  `Sdk.CreateTracerProviderBuilder()` **does** read `OTEL_TRACES_SAMPLER` /
  `OTEL_TRACES_SAMPLER_ARG` on its own whenever `.SetSampler(...)` is not
  called, and that an explicit `.SetSampler(...)` call still overrides the
  env var. This confirms `TracerSetup.cs`'s conditional-skip approach is
  correct, not just assumed.
- Wrote the .NET HOW-TO section (`dotnet/HOW-TO.md` §15, plus the env var
  table in §10): exporter mode table, `MetricExporters`/`ExportIntervalMs`
  code example, and the ASP.NET Core `MapPrometheusScrapingEndpoint()` path
  with `PrometheusMetricsPort = 0` to disable the redundant standalone
  listener. Scrape-side filtering note (`OTEL_HELPER_DISABLED_METRICS`
  applies to all exporters) included per US-5.
- Full suite re-run after all the above: **Python 132 passed / 95.38%
  coverage; Go all tests pass (`-count=1`, 22.4s); .NET 164 passed, 0 failed,
  0 warnings** (159 + 5 new sampler tests).

**Resolved after PR 2 opened:**
- PR 2 (https://github.com/StaffOps/otel-libs/pull/2) was retargeted to
  `main` (its stacked base on PR 1 doesn't trigger CI — `ci.yml`'s
  `pull_request` trigger is filtered to `branches: [main]`) and
  closed/reopened to force a fresh CI run. Result: **all 12 checks green**,
  including both `.NET 8.0.x (test + coverage)` and `.NET 10.0.x (test +
  coverage)` jobs — which run with the exact `/p:CollectCoverage=true
  /p:Threshold=90` flags that couldn't be reproduced locally. **The 90%
  coverage gate is now confirmed passing**, closing the caveat that was open
  when this branch was first pushed.
- Phase 5 docs completed: Python and Go HOW-TOs got the same "Metrics without
  a Collector" dual-mode section .NET has (§14/§13 respectively — mount
  example, multi-worker guidance, scrape-side filtering note); root
  `README.md` env var table + precedence rule; root `CHANGELOG.md` entry
  under Unreleased. Steering (`.kiro/steering/project-conventions.md`) was
  re-checked and remains accurate against the shipped code — no changes
  needed.

**Bottom line on "were these steps really done?":** Yes, across the board —
Python and Go were independently re-executed (not recalled), .NET was
verified twice via Docker locally (compiling + passing tests, before and
after the warning fixes) and then a third time via the actual GitHub Actions
CI run, which also closed the one remaining gap: the .NET coverage-threshold
gate.

---

## Phase 1: Options & Contract Resolution

### Task 1.1: Python options ✅
**Covers:** US-1, US-2 (resolution)

- [x] `TelemetryOptions.metric_exporters: list[str] | None = None` (`python/otel_helper/config.py`)
- [x] Resolve in `resolve_from_env()`: explicit > `OTEL_METRICS_EXPORTER` > legacy (endpoint→otlp, else prometheus). Parse comma-separated, case-insensitive, trimmed
- [x] `TelemetryOptions.export_interval_ms`: explicit > `OTEL_METRIC_EXPORT_INTERVAL` > 30000
- [x] `validate()`: unknown exporter value → ValueError listing valid values; `otlp` without endpoint → ValueError
- [x] `none` resolves to empty list (metrics signal disabled)

### Task 1.2: Go options ✅
**Covers:** US-1, US-2 (resolution)

- [x] `Options.MetricExporters []string` + `WithMetricExporters(...string)` functional option (`go/options.go`, `go/config.go`)
- [x] Same resolution/validation as 1.1 in `resolveFromEnv()` / `validate()`
- [x] `Options.ExportIntervalMs` honoring `OTEL_METRIC_EXPORT_INTERVAL`

### Task 1.3: .NET options ✅
**Covers:** US-1, US-2 (resolution)

- [x] `TelemetryOptions.MetricExporters` (string, comma-separated) + env resolution in `TelemetryOptionsPostConfigure`
- [x] `TelemetryOptions.ExportIntervalMs` honoring `OTEL_METRIC_EXPORT_INTERVAL` (stop hardcoding 30_000 in `MetricsSetup.cs:29`)
- [x] Validation in `TelemetryOptionsValidator` (unknown value, otlp-without-endpoint)

### Task 1.4: Contract tests (3 langs) ✅
**Covers:** US-6

- [x] Table test: every `OTEL_METRICS_EXPORTER` value of US-1 → expected reader set / validation error (`python/tests/test_metrics_contract.py`, `go/metrics_contract_test.go`, `dotnet/OtelHelper.Tests/MetricsContractTests.cs`)
- [x] Precedence: explicit option beats env; standard env beats legacy inference
- [x] Case/whitespace tolerance; `none` behavior
- [x] .NET verified: `dotnet test` via Docker (164 passed, 0 failed, 0 warnings) + confirmed again in real CI with the 90% coverage gate

---

## Phase 2: Metrics Pipeline — Multi-Reader

### Task 2.1: Python `configure_metrics` ✅
**Depends on:** 1.1 — **Covers:** US-1

- [x] Replace if/else with reader accumulation per resolved exporter list (`python/otel_helper/metrics.py`)
- [x] OTLP reader uses `options.export_interval_ms`
- [x] Prometheus reader added independently of endpoint presence

### Task 2.2: Go `configureMetrics` ✅
**Depends on:** 1.2 — **Covers:** US-1

- [x] Reader accumulation (both branches append `sdkmetric.WithReader`) (`go/metrics.go`)
- [x] Dedicated `prometheus.NewRegistry()` + `promexporter.WithRegisterer` (design D5)

### Task 2.3: .NET `ConfigureMetrics` ✅
**Depends on:** 1.3 — **Covers:** US-1

- [x] `AddOtlpExporter` and `AddPrometheusHttpListener` per resolved list (both allowed on same builder) (`dotnet/OtelHelper/MetricsSetup.cs`)

### Task 2.4: Dual-mode integration tests (3 langs) ✅
**Covers:** US-6

- [x] Same counter visible in in-memory OTLP reader AND scraped from `/metrics` (Prometheus text format), single provider
- [x] `prometheus`-only mode with endpoint set: nothing pushed via OTLP
- [x] .NET verified alongside 1.4 (same test file, same CI run)

---

## Phase 3: Mountable Handlers + Listener Robustness

### Task 3.1: Python handler & listener ✅
**Depends on:** 2.1 — **Covers:** US-3, US-4

- [x] `otel_helper.metrics_app()` (wraps `prometheus_client.make_asgi_app()`); export in `__init__.py`
- [x] `OTEL_HELPER_METRICS_PORT=0` / `prometheus_metrics_port=0` → skip `start_http_server`
- [x] Port-in-use raises from `setup_telemetry()`-reachable path (`configure_metrics`) with message pointing to `metrics_app()`

### Task 3.2: Go handler & listener (absorbs P5) ✅
**Depends on:** 2.2 — **Covers:** US-3, US-4

- [x] `MetricsHandler() http.Handler` using the dedicated registry (`go/metrics.go`)
- [x] Listener: synchronous `net.Listen` (fail-fast on busy port), `ReadHeaderTimeout: 5s`, serve error → `otel.Handle`
- [x] `srv.Shutdown` appended to the composite shutdown chain in `otelhelper.go`
- [x] Port `0` / `WithoutMetricsListener()` → reader active, no listener

### Task 3.3: .NET web-app path ✅
**Covers:** US-3

- [x] HOW-TO section: `Prometheus.AspNetCore` + `MapPrometheusScrapingEndpoint()` for ASP.NET Core apps; `HttpListener` for headless processes (`dotnet/HOW-TO.md` §15)
- [x] `PrometheusMetricsPort = 0` → skip `AddPrometheusHttpListener` (`dotnet/OtelHelper/MetricsSetup.cs`)

### Task 3.4: Handler tests (3 langs) ✅
**Covers:** US-6

- [x] Mounted handler serves Prometheus text format (Python: ASGI test client; Go: `httptest`)
- [x] Port 0 suppresses listener; busy-port failure is loud (Py raise / Go setup error)
- [x] .NET port-0 build test (`MetricsContractTests.cs`) verified passing via Docker + CI

---

## Phase 4: Sampler Precedence

### Task 4.1: `OTEL_TRACES_SAMPLER` respected (3 langs) ✅
**Covers:** US-2

- [x] If `OTEL_TRACES_SAMPLER` env set: helper does not construct its own sampler from `OTEL_HELPER_SAMPLE_RATIO` (SDK env config wins) — `python/otel_helper/tracing.py`, `go/tracing.go`, `dotnet/OtelHelper/TracerSetup.cs` + `TelemetryOptionsPostConfigure.cs`
- [x] Tests: sampler type asserted per combination (helper var only / standard var only / both → standard wins) — `python/tests/test_sampler_precedence.py`, sampler tests in `go/metrics_contract_test.go`, `dotnet/OtelHelper.Tests/SamplerPrecedenceTests.cs` (5 tests)
- [x] .NET: closed the gap — before writing the test, empirically verified (disposable console app in the SDK 8.0 container) that `Sdk.CreateTracerProviderBuilder()` reads `OTEL_TRACES_SAMPLER` on its own when `SetSampler()` isn't called, and that explicit `SetSampler()` still overrides it

---

## Phase 5: Documentation — ✅ DONE

### Task 5.1: HOW-TOs (3 langs) ✅
**Covers:** US-3, US-4, US-5

- [x] "Metrics without a Collector" section: modes table, dual-mode example, mount example (`python/HOW-TO.md` §14, `go/HOW-TO.md` §13, `dotnet/HOW-TO.md` §15)
- [x] Multi-worker guidance (Python: mount or `PROMETHEUS_MULTIPROC_DIR`; Go: `MetricsHandler()` on own mux; .NET: ASP.NET Core mount)
- [x] Scrape-side subset filtering: `metric_relabel_configs` + vmagent example; note that `OTEL_HELPER_DISABLED_METRICS` affects all exporters
- [x] .NET-specific: the ASP.NET Core `MapPrometheusScrapingEndpoint()` section from Task 3.3 included

### Task 5.2: Root README + CHANGELOG ✅
- [x] Env var table: added `OTEL_METRICS_EXPORTER`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_TRACES_SAMPLER`; precedence rule paragraph (root `README.md`)
- [x] CHANGELOG entry under Unreleased (root `CHANGELOG.md` — added as a second entry alongside PR 1's)

### Task 5.3: Steering check ✅
- [x] Confirmed `.kiro/steering/project-conventions.md` matches shipped behavior — read the full file this session, cross-checked every claim (env var precedence, `OTEL_METRICS_EXPORTER` values, sampler precedence, packaging convention) against the actual implementation. Accurate as-is, no changes needed. Committed alongside this work (it predates this session but was still uncommitted).

### Follow-up needed after this spec (not part of it)

`ANALISE-PROBLEMAS.md` items not touched by this spec, still open:
- **P8** — semconv `deployment.environment` (legacy, Go) vs `deployment.environment.name` (current spec) divergence across languages.
- **P9** — .NET resolves `TelemetryOptions` twice (once for DI's options pipeline, once manually in `AddOtelHelper`), a latent trap if they ever diverge.

---

## Requirements Coverage Matrix

| Requirement | Task(s) |
|-------------|---------|
| US-1: `OTEL_METRICS_EXPORTER` contract | 1.1–1.4, 2.1–2.4 |
| US-2: Standard env precedence | 1.1–1.3, 4.1 |
| US-3: Mountable handler | 3.1, 3.2, 3.3 |
| US-4: Listener robustness/lifecycle | 3.1, 3.2 |
| US-5: Scrape-side filtering docs | 5.1 |
| US-6: Parity tests | 1.4, 2.4, 3.4 |

## Estimated Effort

| Phase | Estimate |
|-------|----------|
| 1. Options & contract | 3h |
| 2. Multi-reader pipeline | 3h |
| 3. Handlers & robustness | 4h |
| 4. Sampler precedence | 2h |
| 5. Docs | 2h |
| **Total** | **~14h** |
