# Tasks — Metrics Exporter Contract Implementation Plan

## Status (2026-07-14)

| Phase | Status |
|-------|--------|
| 1. Options & contract resolution (3 langs) | ✅ Done (Python, Go, .NET) |
| 2. Metrics pipeline: multi-reader (3 langs) | ✅ Done (Python, Go, .NET) |
| 3. Mountable handlers + listener robustness | ✅ Done (Python, Go, .NET incl. ASP.NET Core HOW-TO) |
| 4. Standard env precedence (interval, sampler) | ✅ Done (Python, Go, .NET incl. dedicated .NET sampler tests) |
| 5. Docs (HOW-TOs, README, CHANGELOG, steering) | 🟨 .NET HOW-TO §10/§15 done; README/CHANGELOG/other HOW-TOs/steering-check not started |

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

**Caveat — still NOT verified:**
- The CI job also gates on `/p:CollectCoverage=true ... /p:Threshold=90`
  (coverlet.msbuild, 90% line coverage). Reproducing that exact invocation in
  the Docker container on this machine produced inconsistent/empty output
  across several attempts (build succeeded, no visible test report, or fully
  silent with exit 0) — most likely an environment artifact (ARM64 host +
  coverlet.msbuild's native collector, or an MSBuild incremental-build skip),
  not evidence of a real problem, since the plain `dotnet test` runs were
  clean and reproducible every time. **The 90% coverage gate itself has not
  been independently confirmed for the new `.cs` test files** — this will be
  checked by the real CI once the branch is pushed and the PR opened, rather
  than re-fought locally.

**Not started:**
- Phase 5 docs beyond the .NET HOW-TO done above: Python/Go HOW-TOs still
  need the same "Metrics without a Collector" section (they already had a
  Prometheus-fallback section from rc.1 — needs the dual-mode/env-var-table
  update mirroring what .NET got); root `README.md` env var table; root
  `CHANGELOG.md` entry under Unreleased; steering re-check against final
  shipped code (the steering file itself was already accurate when read this
  session — see below).
- No PR opened yet for this branch (PR 2, stacked on the still-unmerged PR 1:
  https://github.com/StaffOps/otel-libs/pull/1).

**Bottom line on "were these steps really done?":** Yes for Python and Go —
independently re-executed, not just recalled. Yes for .NET *compiling and
passing tests, including the newly-added sampler tests* — independently
executed via Docker (this machine has no `dotnet` CLI) both before and after
the warning fixes. Not yet confirmed for the .NET *coverage threshold* —
flag this as open, don't assume it passes; it will surface in real CI.

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

### Task 1.4: Contract tests (3 langs) ✅ Python/Go verified, 🟨 .NET written/unverified
**Covers:** US-6

- [x] Table test: every `OTEL_METRICS_EXPORTER` value of US-1 → expected reader set / validation error (`python/tests/test_metrics_contract.py`, `go/metrics_contract_test.go`, `dotnet/OtelHelper.Tests/MetricsContractTests.cs`)
- [x] Precedence: explicit option beats env; standard env beats legacy inference
- [x] Case/whitespace tolerance; `none` behavior
- [ ] **`dotnet test` not yet run this session** — confirm the .NET contract tests actually pass before treating 1.4 as done

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

### Task 2.4: Dual-mode integration tests (3 langs) ✅ Python/Go verified, 🟨 .NET written/unverified
**Covers:** US-6

- [x] Same counter visible in in-memory OTLP reader AND scraped from `/metrics` (Prometheus text format), single provider
- [x] `prometheus`-only mode with endpoint set: nothing pushed via OTLP
- [ ] .NET: same "not yet run" caveat as 1.4 applies here (same test file)

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

### Task 3.3: .NET web-app path 🟨 code done, HOW-TO doc pending
**Covers:** US-3

- [ ] HOW-TO section: `Prometheus.AspNetCore` + `MapPrometheusScrapingEndpoint()` for ASP.NET Core apps; `HttpListener` for headless processes — **not written yet**
- [x] `PrometheusMetricsPort = 0` → skip `AddPrometheusHttpListener` (`dotnet/OtelHelper/MetricsSetup.cs`)

### Task 3.4: Handler tests (3 langs) ✅ Python/Go verified, 🟨 .NET written/unverified
**Covers:** US-6

- [x] Mounted handler serves Prometheus text format (Python: ASGI test client; Go: `httptest`)
- [x] Port 0 suppresses listener; busy-port failure is loud (Py raise / Go setup error)
- [ ] .NET: same "not yet run" caveat applies (port-0 build test written in `MetricsContractTests.cs`, not executed)

---

## Phase 4: Sampler Precedence

### Task 4.1: `OTEL_TRACES_SAMPLER` respected (3 langs) ✅ Python/Go verified, 🟨 .NET written/unverified
**Covers:** US-2

- [x] If `OTEL_TRACES_SAMPLER` env set: helper does not construct its own sampler from `OTEL_HELPER_SAMPLE_RATIO` (SDK env config wins) — `python/otel_helper/tracing.py`, `go/tracing.go`, `dotnet/OtelHelper/TracerSetup.cs` + `TelemetryOptionsPostConfigure.cs`
- [x] Tests: sampler type asserted per combination (helper var only / standard var only / both → standard wins) — `python/tests/test_sampler_precedence.py`, sampler tests in `go/metrics_contract_test.go`
- [ ] .NET: no dedicated sampler-precedence test written yet (only covered indirectly, if at all, by `MetricsContractTests.cs`) — **gap**, add before closing this task

---

## Phase 5: Documentation — ⬜ NOT STARTED

### Task 5.1: HOW-TOs (3 langs)
**Covers:** US-3, US-4, US-5

- [ ] "Metrics without a Collector" section: modes table, dual-mode example, mount example (`python/HOW-TO.md`, `go/HOW-TO.md`, `dotnet/HOW-TO.md`)
- [ ] Multi-worker guidance (gunicorn: mount or `PROMETHEUS_MULTIPROC_DIR`)
- [ ] Scrape-side subset filtering: `metric_relabel_configs` + vmagent example; note that `OTEL_HELPER_DISABLED_METRICS` affects all exporters
- [ ] .NET-specific: the ASP.NET Core `MapPrometheusScrapingEndpoint()` section from Task 3.3 belongs here

### Task 5.2: Root README + CHANGELOG
- [ ] Env var table: add `OTEL_METRICS_EXPORTER`, `OTEL_METRIC_EXPORT_INTERVAL`; precedence rule paragraph (root `README.md`)
- [ ] CHANGELOG entry under Unreleased (root `CHANGELOG.md` — already has an Unreleased section from PR 1, this spec adds a second entry to it)

### Task 5.3: Steering check
- [ ] Confirm `.kiro/steering/project-conventions.md` matches shipped behavior (table already updated 2026-07-04 anticipating this spec) — note: `project-conventions.md` shows as modified in `git status` on `main`, not yet committed; verify it doesn't conflict with what actually shipped here

### Immediate next steps, in order

1. Run `dotnet test` on `dotnet/OtelHelper.Tests` (working dir has uncommitted
   `.cs` changes across `Models/`, `MetricsSetup.cs`, `TracerSetup.cs`,
   `TelemetryExtensions.cs`, plus the new `MetricsContractTests.cs`). Fix any
   build/test failures before moving on.
2. Add the missing .NET sampler-precedence test (Task 4.1 gap above).
3. Write the .NET HOW-TO section for the ASP.NET Core Prometheus path (Task 3.3).
4. Do Phase 5 docs across all three languages + root README/CHANGELOG/steering.
5. Decide commit granularity (see session notes above) and commit.
6. Push `feat/metrics-exporter-contract` and open PR 2, stacked on PR 1
   (https://github.com/StaffOps/otel-libs/pull/1, still unmerged).
7. After PR 2: PR 3/4 from `ANALISE-PROBLEMAS.md` remain untouched —
   P8 (semconv `deployment.environment` vs `.name` divergence) and P9 (.NET
   options resolved twice) are not part of this spec and still need their own
   work.

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
