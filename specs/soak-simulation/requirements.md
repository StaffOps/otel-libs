# Soak simulation — requirements

**Status**: Draft r4 (2026-10-28) | **Owner**: repo owner | **Relates to**:
[config-contract-v0.3 D10](../config-contract-v0.3/design.md) (rc soak; amended
2026-10-28 to accept this simulation), D4, D5, D11,
[ADR-002](../decisions/ADR-002-parentbased-sampler.md),
[ADR-003](../decisions/ADR-003-environment-unset-not-local.md),
[ADR-006](../decisions/ADR-006-debug-span-attribute.md),
[ADR-007](../decisions/ADR-007-keep-prometheus-in-core.md)

> **r2 (2026-10-28)** after the S0 review (`observability` + `dev`): the
> example apps cannot carry the scenarios (different domains per language,
> Go has no gRPC hop, no DEBUG logs, unbounded in-memory stores), tail
> sampling is any-match not first-match, and several checks could not prove
> what they claimed. r1 is superseded; the main changes are soak-owned apps
> (SR0), an `unset` environment, an isolated prd-debug path, agent-side
> counting, real fault-injection modes, and a soak-specific migration query set.
>
> **r3 (2026-10-28)** after the r2 re-review: `dev`/`hml` export INFO, not
> DEBUG (only debug forces DEBUG); per-environment sampling shares come from
> `count` connectors (the tail-sampling counters have no environment);
> `prd-debug` is judged by the rate limiter's real token-bucket semantics;
> per-app completeness uses calibrated spans-per-route; the inbound-traceparent
> parent is not an orphan; 0.2 apps get their own source, deps and an
> always-sample path; DB rows dropped from SR11.
>
> **r4 (2026-10-28)**: count output made cumulative and verified by a
> positive control; prd-debug judged by bounds with a pre-sampling rate;
> api answers 5xx on `/backend-error`; per-route spans calibrated per inbound
> class; gRPC health probe added; 0.2 endpoint uses an explicit `http://`.

## Context

D10 makes `0.3.0` final conditional on the rc being soaked in DEV/HML; no
such environment exists (owner, 2026-10-28). This spec defines a local
simulation that produces the same evidence: the rc under load for hours,
through a production-shaped Collector pipeline, with failures injected, in
several simulated environments, with every claim checked where it can be
observed. Unit/parity suites and `testing/e2e/smoke.sh` prove the contract per
request; they do not prove behavior over time, under failure, or through a
tail-sampling gateway into real backends.

Everything runs with Docker Compose on one machine. Kubernetes-specific
behavior is **deferred** (Phase K, tasks.md), not dropped.

## Glossary

- **Stack**: Compose project `otel-libs-soak` (`testing/soak/`).
- **Soak apps**: purpose-built Go and Python apps under `testing/soak/apps/`
  with one behavioral contract (SR0) — not the `*/example` apps.
- **Environment / app set**: soak apps started with a given
  `deployment.environment.name` (`dev`, `hml`, `prd`) or none (`unset`);
  `prd-debug` is `prd` with `OTEL_HELPER_DEBUG_LEVEL=true`.
- **Agent / gateway**: the Collector tiers (design topology).
- **Quiesced checkpoint**: load stopped, wait ≥ SDK batch schedule delay +
  export timeout + agent batch timeout, then read counters.
- **Short / full run**: same scenarios; the full run adds the long steady
  phase (design S-D3).

## Requirements

### SR0 — Soak apps with one contract in both languages
- The stack SHALL use soak apps (Go and Python) built on the helper libs only
  through their public API, with identical: routes (`/ok`, `/client-error`
  → 4xx, `/backend-error` → backend returns a gRPC error status, `/health`),
  transports (api → backend over **gRPC**; worker → api over HTTP; the worker
  starts a new trace per call), the api answering **5xx** on `/backend-error`
  (so the entry span carries the error status), a gRPC health service on each
  backend probed by the load generator, custom
  span names, custom instrument names/units/attributes, and log records (one
  DEBUG, INFO, WARN, ERROR record per request type via the helper logging
  path: Go `NewSlogHandler`, Python `logging`).
- Soak apps SHALL hold no unbounded state (fixed-size buffers, size recorded
  in the report), SHALL set no resource attributes in code, SHALL be
  configured only through env vars, and SHALL shut down gracefully on SIGTERM
  within a bounded grace (Python server graceful timeout set explicitly).
- The worker SHALL generate no traffic of its own except when the load
  generator tells it to (no self-load confounding counts).

### SR1 — Production-shaped pipeline
- app → agent Collector → `loadbalancingexporter` (`routing_key: traceID`) →
  two gateway Collectors (traces) → Tempo; metrics and logs agent → one
  gateway → VictoriaMetrics / Loki; Grafana provisioned with the datasources.
- Gateway processors SHALL be ordered `memory_limiter` → `filter` (health,
  D11.7) → `tail_sampling` → `resource` (`gateway.id`) → `batch`.
- The tail-sampling policy SHALL be the literal list in design S-D8
  (any-match semantics stated there).
- Collectors SHALL expose internal telemetry on `0.0.0.0` via
  `service.telemetry.metrics.readers` so the checker can scrape them.
- VictoriaMetrics SHALL run with `-opentelemetry.usePrometheusNaming`,
  `-opentelemetry.promoteAllResourceAttributes` +
  `-opentelemetry.ignoreResourceAttributes` (volatile attributes, D11.6) and
  `-storage.minFreeDiskSpaceBytes`; the report SHALL record that each flag
  was accepted by the pinned version.

### SR2 — Environments and transports
- Per language: `prd` runs api + backend + worker; `dev`, `hml`, `unset` and
  `prd-debug` run api + backend (design S-D11 matrix).
- `unset` sets neither `deployment.environment.name` nor `ENVIRONMENT`.
- `prd-debug` SHALL have its own api + backend and its own agent route, so
  its forced retention (ADR-006: "every trace it touches") cannot
  contaminate the `prd` rates.
- At least one set exports OTLP/gRPC, one OTLP/HTTP, one over TLS with a
  private CA (`OTEL_EXPORTER_OTLP_CERTIFICATE`).
- Only standard env vars SHALL configure the rc apps (`OTEL_SERVICE_NAME`,
  `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_*`, `OTEL_*_EXPORTER`). The
  0.2 apps of SR11 are the only exception and SHALL be listed with their env.

### SR3 — Load
- An **open-loop** (constant arrival rate) load generator SHALL drive each
  api with a fixed mix of the SR0 routes, some requests carrying an inbound
  `traceparent` with sampled=01 and some with sampled=00, and SHALL log each
  request's timestamp, route, status and latency itself (client-side truth
  that does not depend on the telemetry pipeline). Rate and mix are run
  parameters recorded in the report.

### SR4 — Steady state: no leaks, no silent loss
- Under load for the steady phase, per app: anonymous memory (cgroup),
  threads/goroutines and open FDs SHALL NOT show sustained growth (design
  S-D5); no restart or exit; no export error on the diagnostics channel
  outside injected-failure windows.
- Completeness at each quiesced checkpoint, per signal (spans, log records,
  metric data points) and per `service.name` × environment:
  - canary set: items accepted by the agent SHALL equal the items the app
    produced (console output parsed as JSON objects, design S-D6);
  - every other set: agent-accepted spans SHALL equal Σ over k6-logged
    **answered** requests of the calibrated spans per (route × inbound class:
    none / sampled=01 / sampled=00); health excluded;
  - every loss between agent and backends SHALL be accounted for by Collector
    and backend counters (design S-D6 list). Unaccounted loss fails the run.

### SR5 — Collector failure modes
For each mode (design S-D12): **refused** (agent stopped, address kept),
**black hole** (packets dropped, no RST), **hung peer** (agent paused),
**DNS failure** (name removed):
- apps SHALL keep serving: zero request errors attributable to telemetry (from
  the load generator's log), request p99 ≤ pre-failure p99 + export timeout;
- the failure SHALL appear on the diagnostics channel (principle 8);
- after recovery apps SHALL resume exporting within a bound derived from the
  documented retry/backoff at the pinned SDK versions (`unmeasured` until
  S7 measures it; the report states the bound used);
- in black hole / hung modes each export attempt SHALL end within the
  configured `OTEL_EXPORTER_OTLP_TIMEOUT` (ms) + 1 s, measured from
  diagnostic timestamps (design S-D4 names the lines parsed);
- memory SHALL stay bounded (growth stops at the SDK queue limits); the
  report states loss per signal per mode, measured not assumed.

### SR6 — Misconfiguration is loud, never fatal
- WHEN an api starts with a wrong CA, an unreachable endpoint, or a
  protocol/port mismatch THEN it SHALL start and serve, and the problem SHALL
  appear on its diagnostics output within one export interval + export
  timeout.

### SR7 — Graceful shutdown under load
- WHEN an app gets SIGTERM under load (run on `dev`, sampled 100%) THEN it
  SHALL exit within 10 s and the agent SHALL have received the telemetry it
  produced up to the signal (agent `count` connector, quiesced).

### SR8 — Environment-specific behavior
- Logs: `prd` (non-debug) apps SHALL export no record with
  `severity_number` < 13 (WARN); `dev`, `hml` and `unset` SHALL export INFO
  but no DEBUG (`go/logging.go` DEV/HML → Info; `python/otel_helper/config.py`);
  `prd-debug` SHALL export DEBUG (debug forces DEBUG); `unset` SHALL log the
  ADR-003 warning once.
- Sampling (`dev`, `hml`, `prd`, `unset`): the retained share of traces —
  numerator from a post-sampling `count` connector at each gateway, denominator
  and error share from an agent `count` connector on api entry spans (design
  S-D6) — SHALL match the S-D8 formula within the binomial tolerance of S-D5;
  `unset` at the PRD rate (D11.1); error traces retained.
- `prd-debug`: per gateway, over 60 s windows, retained spans/s SHALL lie in
  [max(min(R, λ_g), (0.1 + e)·λ_g), R + (0.1 + e)·λ_g] (+ one 2R burst after
  each idle gap ≥ 2 s), widened by ±3σ for the probabilistic term (traces
  in the window) and one trace's span count for the R term; judged on the
  run aggregate, per-window results informative; λ_g from a pre-sampling
  count at that gateway; with λ_g ≫ R the retained share SHALL be well below 1
  (the D11.2 claim).
- The `debug-nonprod` policy is checked in a short `dev-debug` phase (dev
  with debug on: retention 1, DEBUG logs exported); a debug `hml` set would
  break the hml rate, so none runs.
- Inbound `traceparent` sampled=00, in a dedicated phase with only that
  traffic: the agent span-count delta for every app SHALL be 0 (ParentBased,
  ADR-002), including `prd-debug` (ADR-006 Neutral).
- Health: an agent `count` connector matching exactly `/health` (`url.path`
  or `http.target`; k6 sends it without query or trailing slash) and the
  `grpc.health.v1.Health/*` span names SHALL read 0 (D5, before any filter),
  after a positive control proved the condition matches (S-D6); for the api
  apps `/health` SHALL be counted in `http.server.request.duration`.

### SR9 — Trace integrity through the load-balanced gateways
- For N sampled traces per environment (fetched whole by trace ID after
  `decision_wait` + flush), every span SHALL carry the same `gateway.id`
  (proves trace-ID routing) and no span SHALL have a parent missing from the
  trace (orphan check computed by the checker), except exactly one missing
  parent on the entry span equal to the parent id k6 logged for that request.
- WHEN gateway-1 (traces only) restarts under load THEN the report SHALL
  count split/orphaned traces in the window; outside the window the checks
  above SHALL hold.

### SR10 — Cross-language parity in the backends
- For the same load, Go and Python soak apps SHALL produce the same metric
  names, units and attribute keys in VictoriaMetrics and the same span names
  and kinds in Tempo, except the documented gaps: root README "Known
  emitted-telemetry gaps" (Python gRPC legacy `rpc.*`, no RPC metrics) and the
  language runtime metric families (Go `go.*`, Python `process.*`/system
  metrics) — which SHALL be added to that README list if not already there.
  Any other difference fails the run.

### SR11 — Migration evidence (0.2 → 0.3)
- The stack SHALL run 0.2 soak-equivalent apps (built from the `go/v0.2.1`
  and `v0.2.0` sources, design S-D9) next to the rc apps and evaluate a
  **soak-specific query set** covering every telemetry rename in the 0.3
  CHANGELOG (HTTP semconv, `deployment.environment.name`, metric names,
  environment value case `PRD` → `prd`; DB keys are excluded — SR0
  apps make no DB call, A10 unit tests cover them), reporting per query: data
  for 0.2 / 0.3 / both / neither. The 0.2 phase runs 0.2 and rc apps side by
  side through an always-sample gateway path with **no inbound traceparent**
  (0.2 Python is bare AlwaysOn, 0.2 Go ParentBased), so rows reflect the
  telemetry, not samplers. Evidence
  for consumers, not a gate. (The repo `dashboards/` query .NET-example
  metrics and are out of this scope.)

### SR12 — Restart identity
- WHEN an app restarts THEN the new process SHALL report a new
  `service.instance.id` (D4), and the old instance id SHALL have no samples
  after restart + one export interval (checked over a time range with
  VictoriaMetrics export, not instant queries).

### SR13 — Additional soak scenarios
- **Burst**: a burst above the SDK batch queue (2048 per process), combined
  with the `hung` agent so the queue actually fills, SHALL make drops visible
  (diagnostics or SDK self-observability). A silent drop is recorded as a
  library finding to triage (Go's batch processor may report drops only at
  debug verbosity — verify), not auto-accepted.
- **Backend outage**: Tempo, Loki or VictoriaMetrics down for 60 s — gateway
  queue loss SHALL be accounted (SR4 counters); apps unaffected.
- **Agent back-pressure**: agent `memory_limiter` triggered SHALL be visible
  in agent counters; app diagnostics SHALL show it only when an export
  ultimately fails (SDK retries that succeed are silent by design); apps keep
  serving.
- **Multi-worker Python** (moved from Phase K): gunicorn with 2 workers
  (`post_fork` + `--preload`, SDK lower and latest legs — the D4 matrix;
  `uvicorn --workers` spawns and skips the fork path) SHALL report one
  `service.instance.id` per process (D4) and the report SHALL
  record the VictoriaMetrics series count trend over the run.
- **Prometheus fallback** (ADR-007): one api with no OTLP endpoint SHALL
  serve `/metrics` with the documented names.

### SR14 — Reproducible, bounded, isolated, reported
- `testing/soak/run.sh {up,short,full,report,down,purge}` SHALL run the
  scenarios and write `reports/<UTC>/report.{md,json}` (rc version, commit,
  image digests, `go version -m` / `pip freeze` of the apps, parameters,
  per-scenario verdict with the exact queries and values, resource and disk
  usage, neighbour load).
- Isolation: orchestration SHALL act only through `docker compose -p
  otel-libs-soak`; the checker container SHALL have no Docker socket; host
  ports bind `127.0.0.1` only and are configurable.
- Safety: a static disk/memory pre-flight before the first run; a
  continuous disk monitor that invalidates the run below a floor; a sleep
  guard (`caffeinate`; wall-clock gap → run invalid); an EXIT/INT trap that
  removes injected faults and runs `down --remove-orphans`; `purge` removes
  only this project's volumes, labeled images and dedicated builder; never an
  unscoped prune and never `--rmi all`.
- No data leaves the machine; no credential enters an image layer.

## Acceptance (for `0.3.0` final, D10)
- [ ] A full run on the **last rc** passes SR4–SR10, SR12, SR13 (SR11 report
      produced).
- [ ] Every failure is fixed in the libs (new rc) or recorded as an accepted
      gap with an owner ruling; the `0.3.0` commit differs from the soaked rc
      only in version strings and CHANGELOG (diff checked).
- [ ] Report refuted by `observability` + `dev` agents; linked from D10 and
      the CHANGELOG.

## Out of scope
- Real traffic shapes/volumes and Collector capacity sizing.
- .NET (0.2.x until Phase C) and Node.js (Phase B).
- Kubernetes-specific behavior — Phase K (tasks.md): `k8sattributes`, pod
  identity recipe (D11.4), chart wiring, Istio (D11.5), platform chart values.
