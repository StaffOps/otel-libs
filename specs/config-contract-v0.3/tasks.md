# Tasks — Config Contract & Parity (`0.3.0`)

Branch: `feat/v0.3-config-contract`. Spec: [requirements](requirements.md), [design](design.md).

## Definition of done (every task)

1. Implemented in **Go and Python** with the same fixture cases (or in the one
   language a G-*/P-* item names).
2. Tests via Docker (`go test -race`, `pytest --cov-fail-under=90`); wire-level
   behavior checked against a real Collector with `docker compose`.
3. Independent validation: `observability` (spec / semconv / backend) and `dev`
   (reproduce, idioms) agents try to refute it.
4. `code-review` on the diff.
   Threshold for 3–4: the PR links the reports with **zero open BLOCKING/HIGH**.
5. `CHANGELOG.md` (Unreleased) entry in the same change. The full docs,
   MkDocs and examples pass is deliberately deferred to **A13** so docs
   describe validated behavior only.

## Phase A — Go + Python

| Task | Depends on | Covers | State |
|------|-----------|--------|-------|
| A1 Parity harness + fixture describing current behavior, strict xfails for target; fake OTLP receiver for `export` cases; Python lower/latest CI matrix; replace private `_metric_readers` assertions; `test_propagation.py` → real tests; e2e `docker compose` with Collector | — | ADR-008, D2, P15, P17 (tests part) | ✅ Done 2026-10-28 — 3 validation rounds (observability, dev, code-review: APPROVE), zero open BLOCKING/HIGH. 135 cases; Go 51 pass / Python 55 pass, rest strict xfail by task. |
| A2 Dependency bumps: Go v1.47/v0.72 + gRPC stats-handler API + otelhttp route names; Python lockstep bounds (+ `instrumentation-logging`, approved 2026-10-27) | A1 | ADR-005, G4, G5, D9.a | ✅ Done 2026-10-28 — 2 validation rounds (observability zero BLOCKING/HIGH, code-review APPROVE, dev reproduced incl. external consumer). Also fixed: IPv6 JoinHostPort regression, schema_url (semconv v1.43), x/net CVEs + govulncheck in CI, broken example Dockerfiles. |
| A3 Config contract: Python stable HTTP semconv default (D12); defer OTLP env to SDK + shims (schemeless, portless gRPC, gzip), code-URL `/v1/<signal>`, per-signal "configured", precedence flips, environment/service-name, `service.instance.id`, env-parse warnings + redaction, `OTEL_*_EXPORTER`, `OTEL_PROPAGATORS`, remove `os.Setenv`, Go timeout/compression sentinels; Go parity seam (`installedOptions` + effective metric interval recorded by `Setup`, read by the adapter instead of `newOptions` — A1 review) ; strip URL userinfo for gRPC; Python timeout ms→s shim (D1.e); test enforcing that helper diagnostics go through log/slog / `otel_helper*` loggers (A1 review); re-check `redaction.client_key_with_plaintext` vs Go SDK printing TLS file paths; settle ONE helper-diagnostics channel (design D6 says stderr; Go uses slog.Warn at otelhelper.go and otel.Handle in metrics.go — A2 review L1) and write the rule into AGENTS.md | A1, A2 | R1, R2, R4, R5, R8, R10, G7, G8, G10, D1, D4, D12, ADR-001, ADR-003 | ✅ Done 2026-10-28 — 4 validation rounds (observability, dev, code-review per language); final: code-review APPROVE, dev zero BLOCKING/HIGH. Fixture 181 cases: Go 163 pass, Python 158 pass, no A3 status left. 18 edge-case rulings added to design D1; ADR-001 corrected twice. |
| A4 ParentBased sampler (Python: stop passing a bare sampler) | A3 | R6, D3, ADR-002 | ✅ Done 2026-10-28 — Go + Python (helper parses OTEL_TRACES_SAMPLER/_ARG; ADR-001 owned input). Validated: observability, dev (41/41 sampler cases SAME over 1000 ids), code-review APPROVE. |
| A5 Logging (Python handler migration + level rule, Go per-record handler, uvicorn loggers; Python must call `set_logger_provider` like Go does — `_logs` API users get a no-op today, A3 dev round 2) | A3 | P10, P16, G3, D6 | ✅ Done 2026-10-28 — Go + Python; validated (observability, dev on the wire, code-review); D6 A5 rulings. |
| A6 Lifecycle: Go setup rollback / idempotent shutdown; Python idempotent setup + `shutdown_telemetry()` | A3 | G1, G2, P12 | ✅ Done 2026-10-28 — Go + Python; D13 parity gap (Python set-once globals) documented. Validated: observability, dev (real-process lifecycle + SIGTERM on the wire), code-review APPROVE. |
| A7-go Health sampler + gRPC health (stats handlers) | A2, A4 | R7, D5 | ✅ Done 2026-10-28 — validated with A5. |
| A7-py Health sampler, `instrument_fastapi(app)`, sync gRPC | A4 | R7, P11, P13, D5 | ✅ Done 2026-10-28 — validated with A5. |
| A8 Python dedicated Prometheus registry, `py.typed`, no `_internal` imports | A1 | P14, P17 | ✅ Done 2026-10-28 — dedicated registry, py.typed (CI wheel check), no _internal imports; /metrics status table (both languages). Validated with A4/A6. |
| A9 Deprecation warnings, remove Go `EXTRA_INSTRUMENTATION` (warn if set), `debug=true` on entry spans | A3 | R8, R9, G9, D8, ADR-006 | ✅ Done 2026-10-28 — Go + Python; validated (observability, dev, code-review). |
| A10 Ext modules: Go `db.system.name` mapping + legacy key, redis metrics, tests; Python SQLAlchemy 2.1 re-validation ⚠️ | A2 | G6, P18 | ✅ Done 2026-10-28 — Go ext modules tested (otelsql closeDB API, redis semconv + no command values), Python unsupported-version warning, DB semconv dup default; validated. |
| A11 Cut `0.3.0-rc.1` | **A13** | D10 | ✅ 2026-10-28 — shipped as `0.3.0-rc.2` (tags `v0.3.0-rc.2`, `go/v0.3.0-rc.2`, `go/ext/*/v0.3.0-rc.2`; rc.1's CI failed, fixed in rc.2); per-language tag rule (D10 A11 ruling) |
| A12 Collector/platform prerequisites | — | D11 | External — **blocks the 0.3.0 rollout, not the rc** |
| A14 Python package rename (ADR-011): distribution `staffops-otel-helper`, import `staffops_otel_helper`, loggers `staffops_otel_helper.*`; deprecated `otel_helper` compatibility module (re-exports, submodule aliases, `.pyi` stubs, one `DeprecationWarning` + one log, removed in 1.0); CI, examples, docs; first in `0.3.0rc3` | A13 | ADR-011, D6, D8, D14 | ✅ 2026-10-29 — code-review approved after fixes (2 rounds); remaining LOWs accepted for a deprecated alias (find_spec answers for every `otel_helper.*` name; a sub-ms race window before the finder is installed) |
| A15 Owner: claim `staffops-otel-helper` on PyPI (placeholder registration, never publish there unless decided) so the name cannot be squatted | — | ADR-011 | ☐ Owner action |
| A16 Identity/resource rulings from the Node B3 review (design D1 rows dated 2026-10-29): dedicated option beats the code resource attribute; blank identity values = not provided; code-only `service.name` resolves without warning; helper-owned `OTEL_RESOURCE_ATTRIBUTES` parsing (no raw echo, R8). Go + Python implement them; parity cases added with Go/Python `xfail:A16` flip to `pass`. Also fix the timestamp-flaky Go deprecation test (`go/deprecation_test.go:42`), and pick one rule for `telemetry.sdk.*` override direction + schema URL (Go: SDK wins + semconv schema URL; Python and Node: env wins, no schema URL — Node D-JS14 row) | Node B3 | D1, R5, R8, ADR-003, D4 | ☐ |
| **A13 MANDATORY — full documentation + examples pass** (see below) | A1–A10 done **and validated** | F9, AC-docs | ✅ Done 2026-10-28 — validated by observability + dev + code-review (2 rounds); library defects it surfaced fixed in `efb5088`, docs in `fb78d74` |

### A13 — Documentation and examples (mandatory, last)

Owner requirement (2026-10-27): runs **only after** A1–A10 are implemented and
validated, so the docs describe validated behavior — never a target. Not optional.

- [x] Repo docs match the code: root `README.md` (env table, principles),
      `CONSUMING.md`, `AGENTS.md`, per-language `README.md` / `HOW-TO.md` /
      `TESTS.md`, `CHANGELOG.md` with migration notes (0.2.0 → 0.3.0), and the
      F9 doc-drift list cleared.
- [x] MkDocs site `docs/site/**` and `mkdocs.yml` nav updated to the same
      content (the site mirrors the repo docs — no divergent copies); site builds
      cleanly (`mkdocs build --strict` via Docker).
- [x] **Per language, a runnable example of creating custom signals**: a custom
      span (child span, attributes, events, status/error recording, root span
      helper), a custom metric (counter, histogram, up-down counter/gauge, with
      attributes), and a custom structured log correlated with the active trace
      (trace_id/span_id). Go and Python complete in Phase A; .NET verified for
      its shipped 0.2.x API; Node.js ships with the same set in Phase B.
- [x] Go example `Dockerfile` vs `Dockerfile.demo` are byte-identical apart from comments — give `.demo` a real purpose or consolidate (A2 review L2); fix stale trace trees in go/example/README.md and go/HOW-TO.md after otelhttp v0.72 span names (`HTTP POST` client spans) (A2 review L6).
- [x] README/HOW-TO (Python): explicit warning that `import otel_helper` must precede any `opentelemetry.*` import when `OTEL_PROPAGATORS` has unsupported values, incl. an isort note (`# isort: skip`) — isort orders `opentelemetry` first (A3 dev L9).
- [x] Stale statements found in A3 round 3 (fix in this pass): default endpoint `http://localhost` (README.md:80, go/README.md:31, go/HOW-TO.md:452,697); defaults `my-service`/`LOCAL` (README.md:77-78); `http/json` "fails validation loudly" (README.md:90 — env now warns); env-rule rows missing from go/HOW-TO.md §9 and python/HOW-TO.md (~236): `*_TIMEOUT` ms + clamp, `*_COMPRESSION`, `*_HEADERS` rules, per-signal → generic → default fallback, `*_INSECURE`; python/HOW-TO.md:442 add "per-signal endpoints are used as-is"; go/README.md (~277) still says "SDK uses AlwaysOnSampler (default)" — now ParentBased (A4); migration notes: basic-auth-in-URL → `Authorization=Basic%20<b64>` header, header-key restriction (`x+y` now skipped) and keys lowercased, Python INSECURE before/after table.
- [x] parity/README.md "Notes" still says Go `grpc.health_*_no_span` are `xfail:A7-go` (now pass); AGENTS.md F7 "health-path filtering diverges" line — update once A7-py lands.
- [x] Mark `OTEL_HELPER_EXTRA_INSTRUMENTATION` as deprecated in python/HOW-TO.md (~212-235, ~330) and python/README.md (~42); fix the `ext` ImportError hint ("pip install otel-helper[sql]") when the extra is installed but the library is not (A9/A10).
- [x] Examples are exercised: each runs in `docker compose` against a real
      Collector and the custom span, metric and log are visible in the
      Collector debug exporter output (recorded in the PR).
- [x] Validated like every other task: `observability` + `dev` refutation
      rounds and `code-review`.

## Deferred

| Task | State | Promotion trigger |
|------|-------|------------------|
| B — Node.js lib on the contract (spec `specs/nodejs-otel-helper/` first) | Deferred | A11 shipped (`0.3.0-rc.1` cut) |
| C — .NET catch-up + backlog below, **then C0** (retarget to `net10.0` only, ADR-009) as the last .NET step | Deferred | B shipped, **or** a HIGH .NET item hits production, **or** .NET 8 EOL (2026-11-10) forces a release (ADR-004). Note: the EOL date is 14 days after this spec — expect at least N6 to be pulled forward as a .NET-only patch |

## Phase C backlog — .NET (validated 2026-10-27)

Contract catch-up R1–R9 in .NET, plus:

| ID | Finding | Evidence | Severity |
|----|---------|----------|----------|
| N1 | Raw `ActivitySource`/`Meter` registered in DI; AspNetCore instrumentation subscribes to it instead of `Microsoft.AspNetCore` — server spans carry the service scope ⚠️ (dev only) | `dotnet/OtelHelper/TelemetryExtensions.cs:47-50` | HIGH |
| N2 | `DebugTraceStateProcessor` treats remote-parent spans as roots → `debug=true,debug=true`; propagator drops the whole tracestate downstream | `Tracing/DebugTraceStateProcessor.cs:17` | HIGH — fix decided by ADR-006, lands in Phase C |
| N3 | `StartRootActivity` clears `Activity.Current`, never restores (also when unsampled) | `ActivitySourceExtensions.cs:16` | MEDIUM |
| N4 | Internal `BuildServiceProvider()`: early throw, double construction, late `Configure` ignored. Hybrid deferred design prototyped: 180/180 (baseline 179 + 1 new late-`Configure` test) | `TelemetryExtensions.cs:70-73` | MEDIUM |
| N5 | Only meters named `ServiceName` exported; add `AdditionalMeters` + `ServiceName.*` | `MetricsSetup.cs:20` | MEDIUM |
| N6 | → **C0** (TFM; last step of Phase C, owner order 2026-10-28). Original: `net8.0` only (EOL 2026-11-10); `net8.0;net10.0` + OTel 1.20.1 prototyped 180/180 (same suite); `OtelHelper.sln` fails on sdk 8 today (NETSDK1045) | csproj | MEDIUM |
| N7 | `Prometheus.HttpListener` 1.9.0-beta.2 → 1.20.1-beta.1 (removes `UriPrefixes`); diff `/metrics` output first | `OtelHelper.csproj` | MEDIUM |
| N8 | `OTEL_HELPER_METRICS_PORT` beats explicit code | `dotnet/OtelHelper/Models/TelemetryOptionsPostConfigure.cs:46-48` | LOW |
| N9 | `AddFilter("Microsoft", Error)` hides framework warnings on every provider | `LoggingSetup.cs:51-55` | LOW |
| N10 | `OtelCollectorEndpoint="collector:4317"` passes validation | `TelemetryOptionsValidator.cs:17-19` | LOW |
| N11 | Profiling `Start()` at registration; prerequisites miss `CORECLR_PROFILER(_PATH)`/`LD_PRELOAD` | `OtelHelper.Profiling/*` | LOW |
| N12 | No SourceLink/`Directory.Build.props`; CS1591 blocks warnings-as-errors | csproj | LOW |
| N13 | Rename packages and namespace to `StaffOps.OtelHelper*` and reserve the `StaffOps.` ID prefix on nuget.org (ADR-011) | csproj, all `using OtelHelper` | MEDIUM |

**Contract follow-up (2026-10-29, Node B0 review)**: Go always passes the resolved OTLP timeout to the batch span/log processors (overriding `OTEL_BSP_EXPORT_TIMEOUT` / `OTEL_BLRP_EXPORT_TIMEOUT`); Python lets the SDK read those variables. Pick one rule (Node uses: explicit BSP/BLRP env wins, else the OTLP timeout), add parity cases, align Go/Python.

**Contract follow-up (2026-10-29, Node B2 review)**: parity matchers. The Node runner treats `prefix` / `regex` / `not_contains` on a present non-string value as a mismatch and compiles regexes with the JS `u` flag; Go (`fmt.Sprint`) and Python (`str()`) still stringify. Adopt the string-only rule in `go/parity_match_test.go` and `python/tests/test_parity.py`, and reject inline regex flags in their validators (parity/README.md "Text matchers apply to strings only", regex dialect).

Refuted (do not act): .NET ignoring `OTEL_SDK_DISABLED` — SDK ≥ 1.15 honors it.
