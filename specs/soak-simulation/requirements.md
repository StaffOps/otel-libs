# Soak simulation — requirements

**Status**: Draft r6.2 "lean soak" (2026-11-23, owner ruling after the two
K0c reviews BLOCKED r6.1; r6.2 not yet reviewed — task K0e). History: r6.1
2026-11-20 (owner rulings K0d); r6 2026-11-19; r4 reviewed 2026-10-28; r5
Phase K draft BLOCKED by two K0 reviews | **Owner**: repo owner | **Relates to**:
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
>
> **r5 (2026-11-15, draft, not yet reviewed — task K0)**: Phase K promoted
> onto the shared homelab cluster `k8s01` (tasks.md promotion note; owner
> confirmation K0b). New SR15; *Context* and *Out of scope* amended. SR0–SR14
> and the Acceptance list are unchanged: Phase S still carries the D10
> evidence, Phase K adds Kubernetes-only evidence next to it. Revised the same
> day after the first K0 review (`observability`): K3 sample-time query, a
> request-based budget (the owner raised kubelet `maxPods`, K0b ruling 3), K6
> raw-counter checkpoints, the named namespace-label exception, Loki counts
> by `service_instance_id`.
>
> **r6 (2026-11-19, draft, not yet reviewed — task K0c)** after two
> independent K0 reviews BLOCKED r5, and the owner rulings of 2026-11-19
> (tasks.md K0b2). Phase S (Docker Compose) is **dropped**; Phase K on
> `k8s01` is the only phase and carries the D10 evidence (config-contract D10
> amendment of 2026-11-19). The workload is the demo apps of
> [`demo-environment`](../demo-environment/) in all four languages, driven by
> k6 in a pod (design K-D11); the artifact is the branch HEAD, no rc cut this
> round (S-D9 deviation, K-D16); a dedicated Collector copy in
> `dev-otel-libs` (K-D10, reverses K-D1) gives exact per-service counts and
> the failure modes. SR0–SR14 stay as written for the record; each one's
> Phase K status is the coverage table under Acceptance (a K task,
> `n/a: dropped (owner 2026-11-19)` or `accepted gap`). SR15 gains dated r6
> notes and new bullets (windows, load, counting, sentinels, platform guards,
> failure modes, misconfiguration, shutdown, secret canary, soak rules,
> orchestration, evidence). Acceptance is rewritten in Phase K terms; the r4
> list is kept, marked superseded.
>
> **r6.1 (2026-11-20)** — owner rulings on r6 (tasks.md K0d). AG1–AG5
> signed; AG6–AG10 not accepted and turned into covered work (K8b
> per-severity counts, a K10 burst mode, K8c multi-worker Python, K7 and K4
> promoted). Loaded-window worker interval 10800 s, so counts no longer
> depend on Tempo (the r6 Tempo subtraction is removed); DNS failure only at
> start-up (SR6), the runtime ExternalName switch removed from K10; guard
> floors accepted as dated `unmeasured` initial values; a mid-window platform
> roll invalidates the window unless it is the requested back-pressure flip.
> r6 was never committed, so these are edited in place, each marked
> "r6.1, 2026-11-20".
>
> **r6.2 "lean soak" (2026-11-23)** — owner ruling after two K0c reviews
> BLOCKED r6.1 and estimated 19–28 engineering days for it (tasks.md K0d2).
> Kept: a mesh and platform guard (new SR15 bullet), the 24 h (30 h, K0e-1) passive window
> (leaks, threads, Python FDs, Go goroutines, the .NET thread pool, restarts;
> Node.js container-only), one 2 h loaded window whose counts are **bounds**
> on the span families that are fixed per request (SR15 "Counting in
> bounds"; every other span and every log count is report-only), the
> refused / hung / back-pressure / redirect failure modes, shutdown (K-shut),
> restart identity (K3) and labels (K5). Newly accepted gaps (owner,
> superseding the 2026-11-20 "not accepted"): AG6 per-severity logs, AG7
> burst, AG8 multi-worker Python (DE14 and gunicorn dropped), AG9
> health-span absence (K7), AG10 the K4 mesh toggle, and new AG11 the
> NetworkPolicy black hole. The K0c findings inside that scope are fixed:
> the run loop becomes its own task (K-loop), the counting rules are
> rewritten for series that appear, expire and come back, the worker drain
> and first-drive timing bound the loaded rate and t1, K-shut runs after t2
> in its own k6 run, the hung sink is a `testing/fakeotlp` mode, sentinel
> and runtime-metric names are the measured ones, and a code-freeze rule
> binds the soaked commit. r6 and r6.1 were never committed, so r6.2 edits
> in place; each change is marked "r6.2, 2026-11-23" and the r6 / r6.1 text
> stays as dated history.
>
> **r6.2 owner rulings on its open questions (K0e decisions, 2026-11-23)**:
> the passive window is 30 h (12 h warm-up + 18 h judged); FDs are read in
> all four languages by one named `exec` (`ls /proc/1/fd | wc -l`); AG12 is
> only "no Node.js runtime metrics", accepted; the OTTL-filtered
> fixed-family count metric is requested, with the gateway `span_metrics`
> as fallback; the soaked commit gets a local tag `soak/<run>`.

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
*(Amended 2026-11-15, r5: Phase K is promoted onto a real, shared cluster —
SR15, design K-D1…K-D9. The Compose simulation stays the only place for
tail-sampling, trace-ID routing and agent fault-injection evidence, because
the cluster's gateway runs neither `tail_sampling` nor a
`loadbalancingexporter` — measured, K-D1.)*
*(Amended 2026-11-19, r6: Phase S is dropped (owner ruling 1, tasks.md
K0b2). The evidence comes from Phase K on `k8s01` only. What only the Compose
simulation could show — tail sampling and trace-ID routing at a gateway, TLS
with a private CA, a backend outage, VictoriaMetrics naming — is an
owner-signed accepted gap (Acceptance, "Accepted gaps").)*

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

Terms added 2026-11-19 (r6, Phase K):
- **Copy**: the dedicated Collector `soak-agent` in `dev-otel-libs` (design
  K-D10) between the demo apps and the shared platform agent.
- **Passive window** (K8a): ≥ 24 h (30 h, K0e-1), no k6, the demo worker drives at its own
  rate. **Loaded window** (K8b): 2 h, k6 at the K-cal rate, the worker only
  consumes the queue.
- **Inbound class**: what a k6 request carries — no `traceparent`, a
  `traceparent` with sampled=01, or one with sampled=00.
- **Sentinels**: the Collector and backend loss counters that must stay
  clean for a count verdict to be valid (SR15 "Sentinels").
- **Soak-run object**: an object the run creates, labelled
  `otel-libs/soak-run=<run>`; the only kind of object the run's trap deletes.

Terms added 2026-11-23 (r6.2):
- **Fixed span family**: a span kind or name whose count per request is fixed
  by the request's (route × scenario × inbound class) cell alone — SERVER,
  CONSUMER and PRODUCER spans, the demo-scope spans `demo.order.accept`,
  `demo.order.process`, `send demo:<lang>:orders` and
  `process demo:<lang>:orders`, and the RPC client and server spans. Only
  these are counted against an expected value; every other span (DB, Redis,
  HTTP client, internal) and every log count is report-only.
- **Bound**: a count verdict is an interval check, never an equality: LOSS
  when the counted value is below the lower bound, DUPLICATES when above the
  upper bound (SR15 "Counting in bounds").
- **Mesh and platform guard**: the checks of SR15 "Mesh and platform guard";
  a breach makes a window invalid with reason `platform: mesh`, never FAIL.

## Requirements

> *Amended 2026-11-19 (r6):* SR0–SR14 were written for the Compose
> simulation and are kept as written. Their Phase K status is the coverage
> table under Acceptance; where Phase K checks one, SR15 states the cluster
> form.

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
- *(Amended 2026-11-19, r6: Phase K uses the demo apps
  (`specs/demo-environment/`: worker → api → gRPC backend, shared PostgreSQL
  and Redis queue) in Go, Python, Node.js and .NET instead of soak-owned apps
  (task S2 dropped). Route mapping: `/ok` → scenario `ok` (201);
  `/client-error` → `business_error` (422); `/backend-error` → `db_error`
  (backend `ALREADY_EXISTS`, api 409 — a 4xx) and `unhandled` (api 500, the
  5xx entry span); `/health` → api `GET /health`, backend `grpc.health.v1`.
  Bounded state: ids `ok-<n mod 500>` / `ord-<n mod 500>`, Redis stream
  `MAXLEN ~ 1000`, PostgreSQL on `emptyDir` (demo DE-D4, DE-D7). Self-load:
  in the loaded window the worker runs with `DEMO_INTERVAL_SECONDS=10800`
  and the window lies between two of its drives (SR15 "Windows"), so no
  `demo.drive` trace falls inside; in the passive window the worker is the
  load. Log records: the demo writes INFO and ERROR at the `dev` level, not
  one record per severity; K8b counts records per `severity_number` per app
  against the expected count (r6.1, 2026-11-20: AG6 not accepted).)*

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

### SR15 — Phase K on the shared cluster `k8s01` (added 2026-11-15, r5)
- **Scope.** Phase K SHALL write only namespaced objects in namespace
  `dev-otel-libs` (K9: also in one `staffops*` namespace; the owner creates
  that namespace — its creation is not a Phase K write). It SHALL NOT write to
  `opentelemetry`, `monitoring`, `istio-system`, `kyverno`, to any
  ArgoCD-managed object, or to any cluster-scoped object, with **one named
  exception** (owner ruling K0b(2)): the `istio.io/dataplane-mode` label on
  the cluster-scoped Namespace `dev-otel-libs`, set to `none` for the K4
  window only and set back to `ambient` with `--overwrite` by the run trap —
  never deleted (Kyverno `namespace-ambient-mode` would re-add `ambient` on a
  missing label anyway) (design K-D5). No other label, annotation or field of
  that Namespace is written.
  *(r6, 2026-11-19: K4 and K9 are deferred (tasks.md), so the label
  exception and the `staffops*` namespace are unused in this round. r6.1,
  2026-11-20: K4 is promoted (AG10 not accepted), so the label exception is
  used again in the K4 window; K9 stays deferred. The copy
  `soak-agent` (Deployment, ConfigMap, Service, PodMonitor) lives in
  `dev-otel-libs` but is GitOps-managed (Argo CD app `soak-agent`, deployed by
  the platform owner's session, design K-D10): the run reads it and never
  writes it; its back-pressure variant is a request to that session. The run
  writes only soak-run objects (label `otel-libs/soak-run=<run>`): the k6 Job
  and its ConfigMap, the ExternalName / no-endpoint Services `soak-otlp*`,
  the sink and redirect pods and their Services, the NetworkPolicy, the
  telemetrygen Jobs, a Service exposing an api's port 9464 for the SR6
  fallback case. It changes the demo apps only through `demo.sh apply` and
  its env-override hook (demo DE-D7 amendment 2026-11-19).)*
  *(r6.2, 2026-11-23, lean soak: K4 is deferred again (AG10 accepted), so no
  step writes the namespace label; the trap only re-reads it and, if it is not
  `ambient`, sets it back with `--overwrite` — a guarded no-op kept for when K4
  is promoted. The soak-run writes are now: the k6 Jobs and their ConfigMaps,
  the `soak-otlp*` Services, the `testing/fakeotlp` sink pods (hung and
  redirect modes) and their Services, the telemetrygen Job, the SR6 fallback
  Service, and three named writes to the demo apps — `demo.sh apply` with
  the hook (including `DEMO_RESTART_NONCE` to force a roll), `demo.sh scale
  --lang|--all <n>` (demo task DE13), and the deletion of each language's api
  pod for K-shut (`kubectl delete pod <name>`, by name, never by selector).
  The NetworkPolicy is no longer written (black hole dropped, AG11).)*
- **Resource budget.** WHEN a K run starts THEN its pre-flight SHALL read the
  node's `allocatable` (`cpu`, `memory`, `pods`) and the sum of the CPU and
  memory requests of its non-terminated pods, and SHALL refuse to start when
  Σ the run's pod CPU requests > allocatable CPU − requested CPU − the CPU
  reserve, when the same holds for memory, or when the run's pods would leave
  fewer free pod slots than the pod reserve (K-D2). WHILE a run is active,
  WHEN a pod outside `dev-otel-libs` that is still `Pending` has a
  `FailedScheduling` event, newer than the run start, whose message contains `Too many pods`, `Insufficient cpu` or `Insufficient
  memory` THEN the run SHALL scale its own Deployments to 0 within one watch
  interval, mark the window invalid and report it — Phase K never starves
  platform workloads.
  *(r6, 2026-11-19: read during r6 with `kubectl describe node k8s01`: 4970 m
  of 8 CPU and 25 456 Mi of 31 941 Mi memory requested, 130 non-terminated
  pods of 200 — ≈ 3.0 cores and ≈ 6.3 GiB free (the owner's estimate:
  ≈ 3.2 cores). All four languages run in the same window when the pre-flight
  fits; rotation is a pre-flight outcome, not the plan. The pre-flight is
  `testing/k8s/demo/preflight.py` and the watch `testing/k8s/demo/schedwatch.py`
  (both exist; the soak reuses them with the soak-run requests — k6, sink and
  redirect pods — added). The copy's 100 m / 256 Mi request is already in the
  node's requested sum.)*
- **Load.** The open-loop load generator SHALL keep the SR3 rules
  (client-side log on its stdout, read with `kubectl logs`) and SHALL drive
  only the apps of the language under test in that window; whether it runs
  in the api's pod or in its own is a K-D2 layout choice.
  *(r6, 2026-11-19: superseded by the "Load" bullet below — k6 in its own
  pod, all languages in one window, no per-request client log.)*
- **Checker.** Every check SHALL query through the API server's service
  proxy (`kubectl get --raw /api/v1/namespaces/<ns>/services/<svc>:<port>/
  proxy/…`) or read-only `kubectl get` / `kubectl logs`, as
  `testing/k8s/verify.py` does: no port-forward, no `exec` (one named
  exception, the K8a FD read below — owner ruling K0e-2, 2026-11-23), no write. A
  query that errors, times out or returns no series for a check that expects
  data SHALL FAIL that check, never pass it (principle 8). The checker SHALL
  never print Collector credentials: CR snapshots drop every `auth` block and
  every authenticator extension before they are written (K-D3).
  *(r6.2, 2026-11-23, owner ruling K0e-2 — named exception: the FD read
  `kubectl exec -n dev-otel-libs <pod> -c <container> -- sh -c 'ls /proc/1/fd
  | wc -l'` in each demo container at every K8a sweep. It is a soak-run read:
  read-only, the fixed command only, demo containers only, never a Collector
  or backing service; every demo image has `sh`, `ls` and `wc` (alpine,
  Debian slim and `aspnet:10.0` bases, read 2026-11-23); K1 checks that PID 1
  is the app process in each container.)*
  *(Corrected 2026-11-23, K-count code review H2: the read runs without a
  shell — `kubectl exec -n dev-otel-libs <pod> -c <container> -- ls -1
  /proc/1/fd` — and the checker counts the listed lines. A non-zero exit is
  an error, never 0 FDs, and an empty listing (0 FDs) is rejected as
  implausible. The pod name must be a demo pod's and the container a DNS-1123
  label; the command is fixed.)*
- **Enrichment (K2).** WHEN a single-container pod in `dev-otel-libs` exports
  THEN its resource SHALL carry `k8s.namespace.name`, `k8s.pod.name`,
  `k8s.node.name`, `k8s.container.name` and `k8s.cluster.name` in Tempo,
  the matching Loki stream labels and Prometheus `target_info` labels; and
  the pod spec SHALL configure only `OTEL_SERVICE_NAME` and
  `OTEL_RESOURCE_ATTRIBUTES` with `deployment.environment.name` (plus the
  documented test tag `otel_libs.run`). Multi-container pods are asserted
  without `k8s.container.name` (K-D4).
  *(Corrected 2026-11-19, r6, review wording: "the pod spec SHALL configure
  only" read as forbidding every other variable, but the demo sets exporter
  and `DEMO_*` variables. Meant: `OTEL_SERVICE_NAME` and
  `OTEL_RESOURCE_ATTRIBUTES` SHALL be the only resource-setting variables.
  Through the copy the shared agent associates each record with the sending
  pod by the `k8s.pod.ip` the copy stamps (`k8s_attributes` passthrough,
  K-D10); K2 asserts that the attributes name the demo pod, not the copy.)*
- **Identity (K3).** WHEN a Deployment is restarted THEN every new process
  SHALL report a new `service.instance.id` (Prometheus `instance`, Loki
  `service_instance_id`), and the timestamp of the last sample of the old
  `instance` SHALL be no later than the old container's
  `status.containerStatuses[].state.terminated.finishedAt` (read before the
  pod object is gone) + one `OTEL_METRIC_EXPORT_INTERVAL`. The sample time is
  read with `max_over_time(timestamp(<metric>{instance="<old>"})[w:<step>])`
  or a raw range selector (`<metric>{instance="<old>"}[w]`), never with
  `timestamp(last_over_time(…))`, which returns the evaluation time
  (measured 2026-11-15). OTLP point timestamps are set at SDK collection, so
  the agent and gateway batch timeouts (5 s each, measured config) are not in
  the bound; they only set how long the check waits for the last points to
  arrive. OTLP-pushed series carry no staleness marker, so an instant query
  still returns them for the Prometheus lookback window.
- **Mesh (K4).** WHEN `dev-otel-libs` is switched from `ambient` to `none`
  and back THEN, per route and inbound class, the spans per request, their
  parent links and the K2 attributes SHALL be equal in both modes, and no
  span SHALL come from the mesh. WHEN the pre-flight finds a waypoint for the
  namespace, a mesh tracing provider or a `Telemetry` object THEN K4 SHALL
  stop: D11.5 ("mesh tracing at 100% or disabled") then applies and K4 needs
  a new review.
  *(Deferred 2026-11-23, r6.2, lean soak: AG10 accepted. Promotion trigger
  in tasks.md.)*
- **Labels (K5).** Prometheus series of a soak app SHALL carry `job` =
  `dev-otel-libs/<service.name>`, `instance` = `service.instance.id` and the
  promoted attributes present on the resource; `deployment.environment.name`
  SHALL be reachable through `target_info` (it is not promoted — measured);
  the report SHALL record the series count per metric before and after each
  rollout restart.
  *(Corrected 2026-11-19, r6: Prometheus `kps` now promotes 13 resource
  attributes, including `deployment.environment.name` and `cluster`
  (`spec.otlp.promoteResourceAttributes`, read 2026-11-19), so the
  `target_info` join is no longer needed. K1 reads the list on every run and
  K5 asserts against the list read, not a list written in this spec.)*
- **Connector signals (K6).** A window is bounded by two quiesced
  checkpoints t1 and t2 (load stopped, wait ≥ SDK batch delay + agent batch
  timeout + gateway batch timeout + the `span_metrics` flush interval of
  30 s, measured config). At each checkpoint the checker reads the raw
  `traces_span_metrics_calls_total{job="dev-otel-libs/<svc>"}` values per
  series (instant query, no `increase()`: its extrapolation makes an exact
  equality impossible); per series the window count is value(t2) −
  value(t1), and a series absent at t1 counts its full t2 value. Σ of those
  counts SHALL equal the load generator's answered requests between t1 and
  t2 × the calibrated non-health spans per route for that service. A counter
  reset (a value lower at t2 than at t1, or a gateway restart between t1 and
  t2) invalidates the window. The service-graph `failed` share per edge SHALL
  match the load generator's error share within the S-D5 binomial tolerance;
  `otelcol_connector_servicegraph_expired_edges` growth is reported, not
  judged. Loki record counts (K6, K8) SHALL count log records selected by
  `service_instance_id` and summed across every stream of the selector —
  Loki splits streams by `__stream_shard__` — never count streams.
  *(Corrected 2026-11-19, r6: two instant reads cannot count exactly. The
  gateway's `span_metrics` drops a series not updated for
  `metrics_expiration: 5m` and a re-created series restarts from its new
  value; its `resource_metrics_key_attributes` include `service.version`, so a
  rebuild makes new series (gateway CR, read 2026-11-19). The quiesced window
  is replaced by the "Counting" and "Sentinels" bullets below; the
  service-graph share is judged on the api → backend edge only.)*
- **Health (K7).** The absence of health spans SHALL be shown on the app side
  (K-D6), not by their absence in Tempo.
  *(Deferred 2026-11-23, r6.2, lean soak: AG9 accepted. Promotion trigger in
  tasks.md.)*
- **Soak (K8).** Under steady load the S-D5 memory and thread rules SHALL hold
  per container on cAdvisor data; restarts SHALL be 0; FD growth is
  reported `n/a` until a source exists (K-D3).
  *(r6, 2026-11-19: superseded by the "Soak rules (K8a)" bullet below.)*
- **Data.** No telemetry leaves the cluster's existing backends; reports
  stay on the operator's machine.
  *(Amended 2026-11-19, r6: a reviewed summary per run is committed — the
  "Evidence" bullet below; raw output stays on the operator's machine.)*

Bullets added 2026-11-19 (r6). They are part of SR15 and carry the cluster
form of SR3–SR7, SR10 and SR12.

- **Windows.** A run has one passive window (K8a: ≥ 24 h (30 h, K0e-1), no k6, the demo
  worker at its own `DEMO_INTERVAL_SECONDS=5`) and one loaded window (K8b:
  2 h, k6 at the K-cal rate, worker `DEMO_INTERVAL_SECONDS=10800`). During
  both, every demo app SHALL export to `soak-otlp` (a soak-run ExternalName
  Service pointing at `soak-agent.dev-otel-libs.svc.cluster.local`), set
  through the demo env-override hook (`OTEL_EXPORTER_OTLP_ENDPOINT`; demo
  DE-D7 amendment 2026-11-19). All four languages run in the same window when
  the pre-flight fits; otherwise one language per window, each with the same
  load parameters. A window SHALL be valid only when every sentinel and every
  platform guard stayed clean and the orchestration saw no wall-clock gap;
  an invalid window is reported with its reason, never passed.
  *(r6.1, 2026-11-20, owner ruling 2:)* the loaded window [t1, t2] SHALL lie
  strictly between two worker drives: t1 after the worker container's
  `state.running.startedAt` (read with `kubectl_json`) plus, for a language
  whose worker drives at start, that drive plus the quiesce; t2 + quiesce
  before `startedAt` + 10800 s; the worker's restart count unchanged over the
  window. Go drives first at start + interval (`go/demo/worker/main.go:109-121`,
  a `time.Ticker`); the other languages' first-drive timing is read at K-cal.
  *(r6.1, 2026-11-20, owner ruling 5:)* WHEN a platform pod the window relies
  on (`soak-agent`, the shared agent or gateway, Tempo, Loki, Prometheus)
  changes name or start time during a window THEN the report SHALL flag the
  roll, and the window SHALL be invalid unless the roll is the requested
  back-pressure flip of `soak-agent` inside its K10 mode.
  *(r6.2, 2026-11-23: the first-drive sentence above is corrected — Python,
  Node.js and .NET drive immediately once their api answers, only Go waits
  one interval — and the window bounds, the drain rule and t1 move to the
  "Windows and drain (r6.2)" bullet below; the roll rule stays and now also
  names ztunnel, istio-cni-node, istiod and kube-state-metrics.)*
- **Load.** WHEN the loaded window runs THEN k6 SHALL run as a soak-run Job in
  `dev-otel-libs` (image `grafana/k6` from docker.io pinned by digest;
  `activeDeadlineSeconds` = window + margin; explicit requests counted by the
  pre-flight) with the `constant-arrival-rate` executor (open loop), driving
  only `POST /orders` on each language's api with bounded order ids
  (`ok-<n mod 500>`, `ord-<n mod 500>`), a fixed scenario mix (`ok`,
  `business_error`, `db_error`, `unhandled`; shares recorded) and a fixed mix
  of inbound classes (none / sampled=01 / sampled=00, random trace ids). k6
  SHALL end with `dropped_iterations` = 0 and SHALL write a `handleSummary`
  JSON with counters per language × route × scenario × HTTP status × inbound
  class, requests with no answer (status 0) included. A non-zero
  `dropped_iterations` invalidates the window; a non-zero no-answer count
  turns that cell's exact verdicts into bounds [answered, answered + no
  answer]. No per-request log is kept (pod log rotation would cut it); client
  latency comes from the summary's per-route percentiles.
  *(r6.2, 2026-11-23: the k6 rules of the "Load (r6.2)" bullet below are
  added — thresholds per cell, `maxVUs`, the deadline, the summary check
  against the pinned k6 and the orphan reservoir; the no-answer bound rule is
  kept.)*
- **Counting (K6).** WHEN a loaded window closes THEN, per `service_name`,
  the copy's `otel_libs_soak_spans_total` increase over [t1, t2] SHALL equal
  the expected count: Σ over (route × scenario × inbound class) of
  k6-answered requests × the spans per request K-cal measured for that
  service. No worker drive falls inside the window (Windows, r6.1), the
  polls make no span (`XREADGROUP` untraced, demo DE-D11 r3.6) and start-up
  spans precede t1, so nothing is subtracted and the count needs no Tempo
  read. *(r6.1, 2026-11-20: the r6 subtraction of worker-driven traces found
  in Tempo is removed, owner ruling 2.)*
  WHEN a loaded window closes THEN, per app and per `severity_number`, the
  Loki records selected by `service_instance_id` (summed across
  `__stream_shard__` streams) SHALL equal the expected records per severity,
  calibrated per (route × scenario × class) at K-cal (r6.1: AG6 not
  accepted).
  `otel_libs_soak_log_records_total` SHALL equal the expected records,
  calibrated the same way and including the correlated ERROR records of A36
  (failed DB spans) and A37 (failed server spans, written whether the span is
  sampled or not, so the sampled=00 class has records and no spans). A count
  above expected is a DUPLICATES verdict, below a LOSS verdict — reported
  separately. The copy's counts are read as raw samples over [t1, t2]
  (`<metric>[w]` evaluated at t2, no `increase()`): per series Σ of positive
  differences, a lower value counts as a reset and its value is added, a gap
  longer than two scrape intervals (15 s each) while load ran invalidates the
  window. The end-to-end count reads
  `traces_span_metrics_calls_total{job="dev-otel-libs/<svc>"}` the same way;
  series that differ only by `service.version` are summed, and a series
  that expired (no sample for `metrics_expiration`) and came back is a reset,
  not a loss. Loki records are counted by `service_instance_id`, summed
  across `__stream_shard__` streams. The service-graph check judges only the
  api → backend edge per language (failed share vs k6's share of
  gRPC-failing scenarios, S-D5 binomial tolerance); every other edge is
  report-only.
  *(Superseded 2026-11-23, r6.2: exact equality cannot hold — non-fixed span
  families vary per request, the copy series expire after 5 min and come
  back, and several series carry one service. Replaced by "Counting in
  bounds" below; the per-`severity_number` Loki counts are AG6, accepted, and
  every log count is report-only.)*
- **Sentinels.** A count verdict SHALL be valid only when, between t1 and
  t2, for job `dev-otel-libs/soak-agent` and for the shared agent and gateway
  jobs: `otelcol_receiver_refused_*`, `otelcol_receiver_failed_*`,
  `otelcol_exporter_send_failed_*` and `otelcol_exporter_enqueue_failed_*`
  did not increase; `otelcol_exporter_queue_size` read 0 at both checkpoints;
  `otelcol_process_uptime` grew monotonically for the copy and the gateway,
  and the gateway pod's name and `status.startTime` did not change (the
  gateway-restart detector, design K-D12); Tempo `tempo_discarded_spans_total`
  and Loki `loki_discarded_samples_total` did not increase. An absent error
  counter counts as 0 only when a success counter of the same job
  (`otelcol_receiver_accepted_*` or `otelcol_exporter_sent_*`) is present in
  the same read; otherwise the sentinel is unreadable and the window invalid.
  *(Superseded 2026-11-23, r6.2, by "Sentinels (r6.2)" below: the otelcol
  names carry no `_total` suffix (measured), the Tempo and Loki siblings are
  named, the queue rule differs for the copy and the shared Collectors, and
  the gateway-restart detector reads `restartCount` and `startedAt`.)*
- **Platform guards.** WHILE a window is active, WHEN between two guard reads
  (every 60 s, an initial value) a refused, failed, send-failed or
  enqueue-failed counter at the shared agent or gateway increases, a
  `memory_limiter` refusal appears there, Tempo or Loki discards increase,
  node real use leaves less free than its floor (`node_memory_MemAvailable_bytes`,
  and the idle share of `node_cpu_seconds_total{mode="idle"}`), or
  `node_filesystem_avail_bytes{mountpoint="/"}` falls below its floor, THEN
  the run SHALL delete the k6 Job within one guard interval, mark the window
  invalid and report which guard fired. In the passive window there is no
  k6: the window is marked invalid, and on the node memory or disk floor the
  demo apps are scaled to 0. The floors are dated `unmeasured` initial values
  (design K-D13), accepted by the owner on 2026-11-20 (ruling 4) and adjusted
  after K-cal. Absent counters follow the Sentinels rule. The
  `FailedScheduling` watch (Resource budget) runs alongside.
  *(r6.2, 2026-11-23: the "Mesh and platform guard" bullet below runs at
  every guard read too; its breach is reason `platform: mesh`. The floors are
  set at K1 from a 1 h reading, design K-D13 r6.2 note.)*
- **Failure modes (K10).** Each mode runs in its own short window after the
  loaded window, k6 at the K-cal rate, the apps exporting to `soak-otlp`.
  WHEN a mode is injected THEN, per app: (1) k6 SHALL count no request whose
  status differs from its scenario's expected status; (2) each route's k6
  p99 SHALL be ≤ its pre-failure p99 + `OTEL_EXPORTER_OTLP_TIMEOUT`; (3) the
  failure SHALL appear on the app's diagnostics channel (stderr through
  `kubectl logs`: the language's helper line or SDK error line, patterns
  fixed in K10); (4) in the hung, back-pressure and black-hole modes each
  export attempt SHALL end within `OTEL_EXPORTER_OTLP_TIMEOUT` + 1 s,
  measured from diagnostic timestamps; (5) after the mode is removed the app
  SHALL resume exporting (its copy counts grow) within the resume bound — the
  documented retry and backoff of the language's pinned SDK, `unmeasured`
  until K-cal measures it, the bound used stated in the report; (6) container
  working set SHALL stop growing at the SDK queue limits (S-D5 rule over the
  mode window); (7) the report SHALL state loss per signal per mode, measured
  against the copy counts, never assumed. WHEN a mode produces no export
  failure THEN it SHALL be reported `not injected`, never passed. Modes
  (r6.1, 2026-11-20, owner ruling 3: the runtime DNS mode — an ExternalName
  switch plus a copy roll — is removed; DNS failure is checked only at
  start-up, under "Misconfiguration". Established connections survive a
  change of `soak-otlp`, so after the refused and hung switches the apps of
  the languages under test restart with unchanged values and the mode starts
  at process start; resume is measured after the switch back, design K-D14):
  - *refused*: `soak-otlp` replaced by a selector-less Service with no
    endpoints (what the client sees under ambient is `unmeasured`);
  - *hung*: a soak-run sink pod that accepts TCP on 4317 / 4318 and never
    reads;
  - *burst with a hung peer* (r6.1, 2026-11-20: AG7 not accepted): while the
    apps export to the hung sink, a k6 burst above the SDK batch queue (2048
    items per process; the burst size per language from the K-cal spans per
    request) SHALL make the drops visible on the diagnostics channel or in
    SDK self-observability; a silent drop is a library finding to triage,
    never accepted by default (SR13);
  - *back-pressure*: the platform session's `soak-agent backpressure on`
    (`memory_limiter` 64 / 16 MiB); the flip rolls the copy pod, which is part
    of the mode (new pod, uptime reset); `otelcol_processor_memory_limiter_*`
    and the receiver refused counters of the copy SHALL show it, and app
    diagnostics only when an export finally fails;
  - *redirect* (A31): one language at a time switched to `http/protobuf`
    against a soak-run pod answering 307 with a `Location` that carries a
    fake token; each app SHALL write one ERROR per signal and status and
    never the `Location` (container logs and Loki scanned for the token). The
    OTLP/gRPC 3xx line is A51 (open): `xfail:A51` for Go, Python, Node.js;
  - *black hole*: an egress NetworkPolicy selecting only the pods of the
    languages under test (by `otel-libs/lang`), denying egress to the copy;
    if ambient does not enforce it, the mode is `not injected` (K-D14;
    accepted by the owner 2026-11-20, ruling 4).
  The demo apps are scaled to 0 only for a namespace-wide toggle (K4); no
  K10 mode needs one.
  *(r6.2, 2026-11-23, lean soak: the burst mode is deferred (AG7) and the
  black hole is dropped (AG11); the hung sink is `testing/fakeotlp --hang`;
  the back-pressure window, criterion 4 and the resume bound follow
  "Failure modes (r6.2)" below.)*
- **Multi-worker Python (K8c; r6.1, 2026-11-20: AG8 not accepted).** WHEN one
  Python api pod runs gunicorn with N ≥ 2 Uvicorn workers (`--preload` with
  the helper's `post_fork` setup, demo task DE14) under k6 load THEN each
  worker process SHALL report its own `service.instance.id` (N distinct
  values in the copy's and Loki's records for that pod, D4), and the sum of
  the per-worker counts SHALL equal the expected count for that api as in
  "Counting"; the report records the per-worker counts and the Prometheus
  series count per worker.
  *(Deferred 2026-11-23, r6.2, lean soak: AG8 accepted; DE14 dropped, no
  gunicorn dependency.)*
- **Health spans (K7; r6.1, 2026-11-20: AG9 not accepted).** The Health (K7)
  bullet above applies; K7 is no longer deferred.
  *(Superseded 2026-11-23, r6.2: K7 deferred again, AG9 accepted.)*
- **Mesh (K4; r6.1, 2026-11-20: AG10 not accepted).** The Mesh (K4) bullet
  above applies, in its own window after K10, with the demo apps scaled to 0
  while the namespace label is switched (pods restarted in each mode).
  *(Superseded 2026-11-23, r6.2: K4 deferred again, AG10 accepted.)*
- **Misconfiguration (SR6 on `k8s01`).** WHEN one language's api restarts
  (env-override hook) with an OTLP endpoint whose DNS name does not exist —
  the only DNS-failure case (r6.1, 2026-11-20, owner ruling 3) — or
  with `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` against the copy's port 4318, THEN
  it SHALL become Ready and answer k6, and the problem SHALL appear on its
  diagnostics output within one `OTEL_METRIC_EXPORT_INTERVAL` +
  `OTEL_EXPORTER_OTLP_TIMEOUT`. WHEN it restarts with no OTLP endpoint THEN
  it SHALL serve `/metrics` on port 9464 with the documented names (ADR-007,
  SR13 fallback), read through the service proxy of a soak-run Service. A
  wrong CA is accepted gap AG1.
- **Shutdown (SR7 on `k8s01`).** WHEN, at the end of the loaded window with k6
  still running, the api pod of each language is deleted (default grace, the
  pod's `terminationGracePeriodSeconds`) THEN the container SHALL exit within
  the grace (`state.terminated.finishedAt` − deletion time; exit code
  recorded), and the copy's span count for that api over [t1, SIGTERM + grace]
  SHALL be ≥ the k6-answered requests for that language issued before the
  SIGTERM × the calibrated api spans — the excess at most the requests
  answered during the grace. k6 splits the counters at the SIGTERM by two
  scenarios with `startTime` (tag `phase=pre|post`). The api Deployment's
  `Recreate` strategy means one api pod at a time, so the service count is
  the old pod's count. Sentinels SHALL be clean.
  *(Superseded 2026-11-23, r6.2, by "Shutdown (r6.2)" below. Corrected: the
  sentence "`Recreate` strategy means one api pod at a time" is false for a
  deleted pod — the Kubernetes Deployment documentation states that
  `Recreate` "will only guarantee Pod termination previous to creation for
  upgrades" and that for a manually deleted Pod "the replacement will be
  created immediately (even if the old Pod is still in a Terminating state)"
  (kubernetes.io/docs/concepts/workloads/controllers/deployment/#recreate-deployment).
  The old and the new pod overlap, so the count is taken per old
  `service.instance.id`, after t2, in its own k6 run.)*
- **Secret canary (A29).** WHEN a window runs THEN every demo app SHALL carry
  `OTEL_EXPORTER_OTLP_HEADERS=x-soak-canary=<token>` (a fake 48-hex token
  generated per run, set through the hook; the copy ignores the header), and
  the token SHALL appear in no Loki line, label or structured-metadata field
  (scanned with `verify.py` `scan_secret` / `loki_hygiene`), no container log
  and no span attribute of the sampled traces read for checks — including
  during the K10 modes, whose SDK error text is where a header could leak.
  *(r6.2, 2026-11-23: "no container log" means the demo apps' containers and
  the Collectors' (the copy, the shared agent, gateway and `otel-logs`), not
  every container on the node; the token is also scanned in the committed
  summary, "Evidence (r6.2)".)*
- **Soak rules (K8a).** Under the passive window, per container, memory
  (`container_memory_rss` and `container_memory_working_set_bytes`), threads
  (`container_threads`), Go goroutines, the .NET thread pool (thread count and
  queue length) and Python open FDs (the helper's system metrics) SHALL meet
  the S-D5 rule; restarts (`kube_pod_container_status_restarts_total`) SHALL
  be 0. FDs are judged for Python only and are `n/a` for the other languages
  (no source, K-D3). The runtime metric names are read at K1 and stated in
  the report (`unmeasured` here). `verify.py demo` SHALL pass (its matrix,
  `expected.json` xfails allowed) at the start and at the end of the passive
  window. The loaded window applies the same rules, report-only.
  *(r6.2, 2026-11-23: names measured 2026-11-23 with an instant query on the
  homelab Prometheus, jobs `dev-otel-libs/demo-*` — Go `go_goroutine_count`;
  .NET `dotnet_thread_pool_thread_count_total` and
  `dotnet_thread_pool_queue_length_total`; Python
  `process_open_file_descriptor_count` (Python jobs only) and
  `process_runtime_cpython_thread_count`. Node.js is judged on the container
  metrics only (RSS, working set, `container_threads`, restarts). K1 re-reads
  the names each run and fails on a missing one.)*
- **Passive window evidence and gate (r6.2, 2026-11-23; pre-soak analysis).**
  Measured names (2026-11-23): `container_memory_rss`,
  `container_memory_working_set_bytes`, `container_threads`,
  `container_start_time_seconds` (job `kubelet`, `metrics_path=/metrics/cadvisor`),
  `kube_pod_container_status_restarts_total`, `go_goroutine_count`,
  `go_memory_used_bytes`, `dotnet_thread_pool_thread_count_total`,
  `dotnet_gc_last_collection_heap_size_bytes`,
  `process_open_file_descriptor_count`, `process_memory_usage_bytes`.
  - *Judged span* (owner ruling K0e-1, 2026-11-23): K8a SHALL be 30 h = a
    12 h warm-up + 18 h judged; the verdict is the S-D5 slope rule over the
    judged span and over its second half; the 1.10 ratio is report-only
    (design S-D5 recalibration of 2026-11-23: with a 1 h warm-up six
    step-shaped, non-leaking containers failed the ratio over 78.9 h of
    data, and the steps ran out to 12 h). *(Was 24 h = 6 h + 18 h in the
    first r6.2 draft of the same day.)*
  - *FDs* (owner ruling K0e-2, 2026-11-23): judged in all four languages
    from the named `exec` read of "Checker" (`ls /proc/1/fd | wc -l` — now `ls -1 /proc/1/fd`, no shell, 2026-11-23 — per
    demo container per sweep), with the S-D5 slope rule; Python's
    `process_open_file_descriptor_count` is kept as a cross-check. cAdvisor
    exposes no `container_file_descriptors`. *(Was: Python only, proposed
    AG12, in the first r6.2 draft.)*
  - *Node.js*: no `v8js_*` or `nodejs_*` runtime series exist for the demo
    services (read 2026-11-23), so Node.js runtime state (heap, event loop,
    handles) is not judged; its container metrics and FDs are — accepted gap
    AG12.
  - *Coverage gate*: the K8a clock SHALL start only when, over the 30 min
    lookback, the gateway → Prometheus path is flowing
    (`traces_span_metrics_calls_total` grows for every demo job) and, for
    each language that has runtime metrics, ≥ 95 % of the expected export
    intervals (`OTEL_METRIC_EXPORT_INTERVAL`) carry a sample of each judged
    runtime series per process. Below 95 % during the window the judged span
    is invalid with reason `platform: telemetry gap`. Motivation: during the
    2026-11-20 incident runtime metrics and span metrics were absent for 78 h
    (gateway send failures).
    *(Initial value, `unmeasured`, 2026-11-23, K0e review M6: the 95 %
    threshold; reversal action: set it from K1's 24 h baseline.)*
  - *Watch list*: dotnet-worker (step pattern, 0.08 Mi/h residual),
    dotnet-api and the Node.js api and backend (no clean lifetime data yet),
    Python (a simultaneous step at 2026-11-20 13:45Z, probably
    environmental); the summary SHALL discuss each. Limits were not near:
    peak RSS 33 % of the limit; working set peaked at 68 % on go-api (page
    cache).
  - *Tempo selectors*: K1 SHALL read the attribute set Tempo holds for the
    demo services and the checks SHALL use selectors from it; a TraceQL rate
    filter on `service.namespace` returned nothing on 2026-11-23.
- **Orchestration.** The run loop SHALL run on the operator's laptop under
  `caffeinate`, write a heartbeat every guard interval, and mark the active
  window invalid on a wall-clock gap > 3 × that interval. WHEN every look-up
  of one guard read fails (blind) THEN it SHALL delete the k6 Job. An
  EXIT / INT / TERM trap SHALL delete only objects labelled
  `otel-libs/soak-run=<run>` (never the copy, never the demo backing
  services, never by kind alone), then re-apply the demo apps without the
  hook's soak values (`demo.sh apply`, its own pre-flight), so no app is left
  exporting to a deleted Service.
  *(r6.2, 2026-11-23: the loop, trap, cleanup and pre-flight are now one
  implementation task (K-loop); the trap rules of "Orchestration (r6.2)"
  below add to this bullet.)*
- **Evidence.** Each run SHALL produce a committed summary
  `specs/soak-simulation/reports/<run>.md`: run id, commit under test, demo
  image tag and digests, k6 and copy image digests, the K1 snapshot, the k6
  summary counters, each check's verdict with the exact queries, values and
  timestamps, invalid windows and why. Raw output SHALL stay under a
  git-ignored path; no summary carries a credential or the canary token.
  *(r6.2, 2026-11-23: the raw path, the scan before commit and the
  code-freeze rule are "Evidence (r6.2)" below.)*

Bullets added 2026-11-23 (r6.2, lean soak). Where one names an r6 or r6.1
bullet, it replaces that bullet's rule for this round; the older text above
stays as dated history.

- **Mesh and platform guard (r6.2).** The run SHALL evaluate this guard in
  the pre-flight and at every guard read (60 s). A breach SHALL make the
  active window invalid with reason `platform: mesh` — never a FAIL of a
  library check — stop k6 within one guard interval and be reported with the
  item that fired:
  - (a) `workload_manager_active_proxy_count{job="istio-system/ztunnel"}`
    SHALL equal the number of Running pods annotated
    `ambient.istio.io/redirection=enabled` on the node (one node; read with
    `kubectl_json`, all namespaces);
  - (b) `increase(istio_tcp_connections_failed_total{reporter="source"}[60s])`
    SHALL be 0 for every series whose source or destination workload
    namespace is `dev-otel-libs`, `opentelemetry` or `monitoring`;
  - (c) the ztunnel logs since the last read SHALL contain no
    `Connection refused` line to a `:15008` address that involves one of
    those namespaces;
  - (d) the restart counts and container start times of every pod in the
    path SHALL be unchanged: the roll list of "Windows" (`soak-agent`, the
    shared agent and gateway, Tempo, Loki, Prometheus) plus ztunnel,
    istio-cni-node, istiod and kube-state-metrics;
  - (e) before any window starts, (b), (c) and (d) SHALL be clean over a
    30-minute lookback;
  - (f) `up == 1` for both `soak-agent` scrape endpoints (internal telemetry
    and the `:8889` counts) and for the shared agent and gateway jobs;
  - (g) every demo pod SHALL be Ready with its restart count unchanged over
    the 10 min before the window;
  - (h) after the hook points the apps at the copy, the copy count of every
    `service_name` SHALL be above 0 within 60 s.
  Motivating evidence (reported 2026-11-23 with the owner's ruling): an
  istio-cni-node restart on 2026-11-20 left the mesh half-enrolled for three
  days — pods annotated for redirection that ztunnel no longer proxied —
  and nothing in the r6.1 guards would have seen it.
  *(Corrected 2026-11-23, K0e review B2, M1, M2, M6, L4:)* item (b) SHALL
  judge only the run's telemetry edges — demo pods → `soak-agent` and
  `soak-agent` → `otel-agent-collector` — against a per-edge baseline that
  K1 measures and dates. The series counts TCP connections that ztunnel, on
  the source side (`reporter="source"`), failed to establish to the
  destination workload (connect refused, reset or timeout through the
  ztunnel or its HBONE `:15008` tunnel); a rise on a telemetry edge means
  the apps' or the copy's exports could not connect. *(Corrected
  2026-11-23, K0f review:)* more precisely, ztunnel increments it for every
  connection it ends with a failure response flag — connect failure, TLS or
  HTTP/2 handshake failure, authorization or network-policy deny, identity
  or certificate error — including an unclassified error after the
  connection was established; drain closes are not counted. The live series
  carry `response_flags="CONNECT"` (`ConnectionFailure`). Source:
  istio/ztunnel `src/proxy/metrics.rs` (`ConnectionResult::record_internal`),
  read on master; K1 rechecks it at the deployed 1.30.8 tag. The guard's
  effect is unchanged: a mid-stream error on a telemetry edge also
  invalidates the window. Other edges (demo →
  Redis / PostgreSQL, gateway → Prometheus, the rest of the three
  namespaces) are report-only and flagged as findings in the summary, never
  a window invalidation. Item (d) exempts the two requested `soak-agent`
  rolls inside the K10 back-pressure window (flip on, flip off). Item (h)
  applies only at a window's start, not when a K10 mode switches
  `soak-otlp` back. Initial values, `unmeasured`, 2026-11-23, each adjusted
  from K1 / K-cal reads with a dated note (reversal action: replace the
  value with the measured one, or drop the item if it fires with no
  run-caused signal): (e) 30 min lookback, (g) 10 min Ready, (h) 60 s.
  K8a is scheduled only after platform changes have settled — no roll of a
  roll-list pod and no platform-session change in the last 30 min (L4).
- **Windows and drain (r6.2).** Worker first drive, from the code: Go drives
  first at start + `DEMO_INTERVAL_SECONDS` (`time.Ticker`,
  `go/demo/worker/main.go:97`, `:108-121`); Python
  (`python/demo/worker/worker.py:155-169`, `:216-219`), Node.js
  (`nodejs/demo/worker/app.js:91-99`, `:175-184`) and .NET
  (`dotnet/demo/Worker/Driver.cs:91-101`) drive immediately once their api
  answers its health check, then every interval. t1 SHALL follow the
  observed first `demo.drive` of every worker (read in Tempo or the copy
  count step it causes) plus the quiesce, and SHALL be taken at most 4 min
  after the last traffic of each service, so every copy series still exists
  at t1 (the copy's series expire after 5 min, "Counting in bounds"). The
  loaded rate × the share of k6 requests that enqueue a message SHALL be at
  most the worker's measured drain rate per language (initial value: the
  demo polls `XREADGROUP COUNT 10` every `DEMO_POLL_SECONDS=2`, about
  5 msg/s, `unmeasured` until K-cal), or the hook SHALL lower
  `DEMO_POLL_SECONDS` for the window. t2 SHALL wait until each worker's
  CONSUMER count has been unchanged for two polls, plus the quiesce. Drain
  check: over [t1, t2] the PRODUCER `send demo:<lang>:orders` count SHALL
  equal the CONSUMER `process demo:<lang>:orders` count per language.
  *(Corrected 2026-11-23, K0e review H4, H6:)* the "at most 4 min after the
  last traffic" constraint is dropped — the reappearance rule of "K0e
  corrections" handles an expired series. t1 for Python, Node.js and .NET
  follows their observed first `demo.drive`; for Go, whose first drive comes
  at start + `DEMO_INTERVAL_SECONDS` (10800 s in K8b), t1 follows the Go
  worker's `state.running.startedAt` + the quiesce.
- **Load (r6.2).** The k6 script SHALL declare one no-op threshold per cell
  (language × route × scenario × inbound class) on a counter tagged with the
  outcome `expected` (status = the scenario's expected status), `unexpected`
  or `noanswer` (status 0), so the `handleSummary` data carries one
  sub-metric per cell and outcome; whether the pinned k6 version emits those
  sub-metrics as written SHALL be checked against that version (K-loop unit
  test and the K-cal run) before any count uses them. `maxVUs` SHALL be ≥
  rate × (request timeout + p99), with the request timeout ≤ 15 s; the Job's
  `activeDeadlineSeconds` SHALL be ≥ image pull + duration + `gracefulStop` +
  summary write. For the SR9 orphan check `handleSummary` SHALL emit a
  bounded reservoir — the first 50 requests per cell — of {`trace_id`,
  `parent_id`, class}; a sampled=01 request's `parent_id` is k6's injected
  parent, which is not an orphan.
  *(Corrected 2026-11-23, K0e review M5, M6:)* the K-loop unit test SHALL
  prove the reservoir on the pinned k6 version; the named fallback, if the
  summary cannot carry it, is one bounded stdout line per request — the
  first 50 per cell per VU — saved by the trap's `kubectl logs`. Initial
  values, `unmeasured`, 2026-11-23: request timeout ≤ 15 s and the
  reservoir of 50 (reversal action: set from K-cal p99 and from the orphan
  check's needed sample size).
- **Counting in bounds (r6.2; replaces "Counting (K6)").** Only the fixed span
  families (Glossary) are judged; every other span and every log count is
  report-only. Per `service_name` and family, the expected interval is
  [Σ cells answered × k(cell), Σ cells (answered + no answer) × k(cell)],
  with k the K-cal spans per request of that family; a count below the
  interval is LOSS, above it DUPLICATES, reported separately. Series rules,
  for the copy's `otel_libs_soak_spans_total` and the gateway's
  `traces_span_metrics_calls_total`, read as raw samples over (t1, t2]: per
  series the positive differences of consecutive samples are summed; a lower
  value is a reset and adds its value; a series whose first sample falls in
  (t1, t2] adds its first value; a series that reappears after a gap adds v
  when v < its last value before the gap, else v − last. Copy series are
  summed over `pod`, `instance` and `exported_instance` per `service_name`;
  gateway series are summed over `service.version`. Correction of K-D12
  (r6.2): the copy's counts do expire — `deltatocumulative` `max_stale` and
  the `prometheus` exporter's `metric_expiration` were both 5 min (now 30 m, K0e correction below) (the copy's
  ConfigMap sets `deltatocumulative: {}` and no `metric_expiration`, read
  2026-11-23; both defaults are 5 min upstream). t1 is therefore at most
  4 min after the last traffic ("Windows and drain"). The family counts use
  the gateway series (they carry `span_name` and `span_kind`); the copy total
  per service is judged only for a service whose K-cal shows the total fixed
  per cell (below), otherwise it is report-only. In a mode that rolls the
  copy (back-pressure) loss is reported as the bound [expected − counted −
  tail, expected − counted], tail = the old copy pod's increments after its
  last scraped sample (at most one scrape interval × the cell rate × k).
  K-cal SHALL check, per cell, that the copy delta / N and each family's
  gateway delta / N are integers and equal across two repeats of N
  requests; a family (or a copy total) that fails is excluded from the
  verdicts and named in the summary. *(Owner ruling K0e-3, 2026-11-23: the
  option is taken — the platform session is asked for a second,
  OTTL-filtered `count` metric on the copy that counts only the fixed span
  families per sending instance (`service_name`, `service_instance_id`,
  family). Family verdicts SHALL use it once it exists, read with the same
  series rules; until then they fall back to the gateway's
  `traces_span_metrics_calls_total`. K-copy accepts it with the positive
  control; the summary states which source each verdict used.)*
  *(Corrected 2026-11-23, K0e review H1, H2, M3 — superseding the
  K0e-3 note's "per family" and the 5 min expiry above:)* the delivered
  metric is `otel_libs_soak_fixed_spans_total`, labels `service_name`,
  `service_instance_id`, `exported_instance`, `exported_job`, `pod`,
  `instance`, `endpoint=count` (live 2026-11-23; delivery smoke: 10 spans →
  `otel_libs_soak_spans_total` 10, fixed 5). It has **no family label**, so
  the copy verdict is one fixed total per `service_instance_id` (Σ k over
  the fixed families per cell); per-family copy counts are not available.
  The gateway per-family counts (`traces_span_metrics_calls_total` summed by
  `service_name`, `span_kind`, `span_name` over every other label) are a
  cross-check, valid only when the shared sentinels are clean. The copy's
  `delta_to_cumulative` `max_stale` and `metric_expiration` are now both 30 m
  (live 2026-11-23); the gateway's `span_metrics` expiry stays 5 m. If K-cal
  finds a copy total that is not integral, the summary names the cause and
  reports the gateway cross-check for that service. The delivered
  condition matches CLIENT spans with `attributes["rpc.system"] == "grpc"`;
  Go and .NET emit `rpc.system.name` (current semconv), so their gRPC client
  family would be missed — the platform session is asked to add
  `kind == SPAN_KIND_CLIENT and attributes["rpc.system.name"] == "grpc"`, and
  K-copy's positive control sends one CLIENT span with each key.
  *(Updated 2026-11-23, soak-agent `homelab-gitops-apps` 1f56009 + b4c1dae —
  closes K0e H1:)* `fixed_spans` now also counts `kind == SPAN_KIND_CLIENT
  and attributes["rpc.system.name"] == "grpc"`, ORed with the `rpc.system`
  condition. Delivery smoke: a CLIENT gRPC span with `rpc.system` → 4 of 4
  spans fixed; with `rpc.system.name` → 4 of 4; with neither key → 2 of 4
  (the server spans only). The copy's `count` connector drops a span
  silently when a listed attribute is missing and has no default;
  `service.instance.id` now defaults to `"unset"`. Rule: a series with
  `service_instance_id="unset"` from a demo service SHALL be reported as a
  finding — every helper sets `service.instance.id` (contract D4) — and
  SHALL make that service's per-instance verdict invalid (its service-level
  report-only counts stay). Component names are now the non-deprecated
  `k8s_attributes` and `delta_to_cumulative` (behavior unchanged); older
  text that quotes the earlier ConfigMap keeps the names it read.
- **Sentinels (r6.2; replaces "Sentinels").** Names as exposed (measured
  2026-11-23, job `otel-agent-collector-monitoring`): the otelcol counters
  carry no `_total` suffix — `otelcol_receiver_{accepted,refused,failed}_{spans,metric_points,log_records}`,
  `otelcol_exporter_{sent,send_failed,enqueue_failed}_*`,
  `otelcol_exporter_queue_{size,capacity}`, `otelcol_process_uptime`. A
  count verdict SHALL be valid only when, over [t1, t2]: no refused, failed,
  send-failed or enqueue-failed counter of the copy or the shared agent and
  gateway increased; the copy's `otelcol_exporter_queue_size` read 0 at t1
  and t2; the shared Collectors' queue size at t2 was ≤ 10 % of their queue
  capacity (a dated 2026-11-23 `unmeasured` floor, K-cal adjusts it); Tempo
  `tempo_discarded_spans_total` (sibling `tempo_distributor_spans_received_total`,
  job `tempo`) and Loki `loki_discarded_samples_total` (sibling
  `loki_distributor_lines_received_total`, job `monitoring/loki`) did not
  increase; the gateway-restart detector saw no change of the gateway pod's
  name, `status.containerStatuses[].restartCount` or `state.running.startedAt`
  (`kubectl_json` at t1 and t2) and `otelcol_process_uptime` of the gateway
  grew without a drop. The sibling rule stays: an absent error counter is 0
  only when a success counter of the same job is present in the same read
  (the idle copy exposes only its queue gauges, measured 2026-11-23).
  *(Corrected 2026-11-23, K0e review B1, H5:)* scope by verdict — the
  copy's `count` connector counts before export, so the copy's own
  sentinels (receiver refused / failed, `memory_limiter` refusals, queue 0)
  gate the copy count verdicts; the shared agent and gateway sentinels and
  the Tempo / Loki discards gate only the end-to-end checks (gateway
  `span_metrics` cross-check, SR9 orphans, SR10, the service-graph share).
  A dirty shared sentinel invalidates those checks, not the copy verdicts
  or the passive window. Every query on a shared job SHALL carry
  `cluster="k8s01"` (the Prometheus also holds `cluster="vps"` series,
  read 2026-11-23), or a series of the other cluster gives a false pass or
  a false invalid.
- **Failure modes (r6.2; replaces the K10 mode list).** Modes: *refused*,
  *hung*, *back-pressure*, *redirect* (A31). *Burst with a hung peer* is
  deferred (AG7); the *black hole* is dropped (AG11). The K10 criteria
  (1)–(3) and (5)–(7) stay, with these changes:
  - *hung*: a soak-run `testing/fakeotlp` pod in a new `--hang` mode that
    completes the TCP and HTTP/2 (h2c) handshake, accepts the gRPC stream or
    HTTP request, and blocks the Export until the client gives up; image
    built from `testing/fakeotlp/Dockerfile` with the builder and platform
    flags of `testing/k8s/build-images.sh`, pushed to
    `harbor.staffops.space/development/otel-libs-fakeotlp:<tag>`, referenced
    by digest (task K-hang). *redirect* uses the same image's existing
    307 path `/parity-redirect-c/`, whose `Location` carries the fixed
    token `parity-redirect-token`;
  - *refused* and *hung*: after the `soak-otlp` switch the apps under test
    restart through the hook with a new `DEMO_RESTART_NONCE` and otherwise
    unchanged values (K-D14 r6.1 note);
  - *back-pressure*: the mode window SHALL start when the new copy pod is
    Ready and the first `memory_limiter` refusal of the copy appears, and
    SHALL end at the next copy pod Ready (the flip back); the pod names are
    recorded. The delivery's "about 1–2 min after the push" is `unmeasured`;
  - criterion (4) uses a proxy: per app and signal, the interval between
    consecutive failure lines on the diagnostics channel SHALL be ≤ the
    batch schedule delay + `OTEL_EXPORTER_OTLP_TIMEOUT` + 1 s (an export
    attempt's start is not logged);
  - criterion (5): the resume bound starts from the upstream retry and
    backoff of each language's pinned SDK (source and value stated per
    language in the summary) and is replaced by the value measured in the
    first mode that runs.
- **Shutdown (r6.2; replaces "Shutdown (SR7 on `k8s01`)").** K-shut SHALL run
  after t2, in its own k6 run with two scenarios tagged `phase=pre|post` and
  its own baseline t1' (the "Counting in bounds" rules). Before the deletion
  the run SHALL record each api's old `service.instance.id`. Each api pod is
  deleted by name at the `pre`/`post` boundary + δ, δ read from the k6
  container's `state.running.startedAt` (the scenario clock starts at k6's
  start, not at the Job's creation). The container SHALL exit within its
  grace (`finishedAt` − deletion time, exit code recorded). The old process's
  count SHALL be read only from copy series with `exported_instance` = the
  old `service.instance.id` (K1 SHALL verify that this label exists on the
  copy series; on 2026-11-23 `otel_libs_soak_spans_total` had no series in
  the last 2 days — the copy was idle — so it is `unmeasured`), and SHALL lie
  in [pre-answered × k, (pre-answered + post requests answered before the
  old container's `finishedAt`) × k] for an api whose copy total K-cal
  qualified; otherwise the K-shut count is report-only and the exit-within-
  grace and the sentinels are the verdict.
  *(Corrected 2026-11-23, K0e review H2, H3:)* `exported_instance` is
  measured (2026-11-23). The old process's count SHALL be read from
  `otel_libs_soak_fixed_spans_total{service_instance_id="<old>"}` with the
  fixed-family k of the api; the upper bound adds the no-answer requests of
  both phases issued before `finishedAt` × k.
- **Restart mechanics (r6.2).** A roll SHALL be forced through the hook by a
  new `DEMO_RESTART_NONCE` value (an allowed `DEMO_*` name, demo task DE13);
  scaling uses `demo.sh scale --lang|--all <n>` (DE13). The hook's allow-list
  SHALL deny `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`, so the run
  cannot change the identity it checks (K2, K3, K5). K3 identity is read on
  the pods of the nonce roll.
- **Orchestration (r6.2).** The run loop (task K-loop, `testing/k8s/soak/`)
  SHALL: refuse to start while any object labelled `otel-libs/soak-run`
  exists, of any run id; on EXIT / INT / TERM first save
  `kubectl logs job/<k6 job>` of every k6 Job of the run to the raw
  directory, then delete only `otel-libs/soak-run=<run>` objects, re-read
  the namespace label and restore it if it is not `ambient`, and re-apply
  the demo apps without the soak values; offer `cleanup --run <id>` with the
  same steps for a run whose laptop was cut off. When the laptop is cut off
  the k6 Job's `activeDeadlineSeconds` is the bound on the load.
- **Floors and capacity (r6.2).** K1 SHALL record node MemAvailable, idle CPU
  and root filesystem free space for 1 h and set the K-D13 floors from that
  reading (dated note); K1 SHALL record the Prometheus PVC capacity against
  its request (reported 20Gi bound vs 60Gi requested, to be read at K1),
  because the 24 h (30 h, K0e-1) passive window's raw range reads depend on its retention.
- **Evidence (r6.2).** Raw output SHALL go to `testing/k8s/soak/runs/<run>/`,
  git-ignored (the `.gitignore` line is an implementation task, K-loop).
  Before the summary is committed, `scan_secret` SHALL run over
  `reports/<run>.md` with the run's canary token and `parity-redirect-token`
  and find neither. Code freeze: the soaked commit SHALL carry a local git
  tag `soak/<run>` (owner ruling K0e-4, 2026-11-23: the run creates it
  locally; the owner pushes tags); from the start of K8a to K11
  no change SHALL land under `go/`, `python/`, `nodejs/packages/` or
  `dotnet/` (checked with `git diff soak/<run>..HEAD` on those paths at K11), or
  the loaded window is re-run on the new commit.
  *(Corrected 2026-11-23, K0e review M4:)* the freeze diff SHALL run over
  `testing/k8s/demo/demo-tag.sh`'s input list (it includes `nodejs/demo/` and the
  `nodejs/package*.json` files, beyond the four library paths), and K8a and
  K11 SHALL check that the running image tags equal `demo-tag.sh` at
  `soak/<run>`. A change inside that list re-runs the loaded window, and
  also the passive window when it changes runtime behavior (K-D16
  reversal).
- **K0e corrections (r6.2, 2026-11-23).** After the K0e review (NOT READY:
  2 BLOCKING, 6 HIGH). They take precedence over the r6.2 bullets above.
  - *Baseline and entry gate (B1)*: K1 SHALL read a 24 h baseline of every
    sentinel and guard counter (shared agent and gateway refused / failed /
    send-failed / enqueue-failed, `memory_limiter` refusals, Tempo and Loki
    discards, the per-edge `istio_tcp_connections_failed_total`). Before
    K8a, K-cal, K8b and each K10 mode, those counters SHALL be clean over
    the 30 min lookback; a failure does not start the window and points to
    the platform session. Measured 2026-11-23 (coordinator, Prometheus MCP):
    `increase(…[15m])` was 0 for the refused / send-failed counters and for
    `istio_tcp_connections_failed_total` after the platform fix; the
    reviewer's 1–3 h windows had covered the broken-mesh period.
  - *Reappearance (H4)*: a series gap at least as long as its source's
    expiry (gateway `span_metrics` 5 m, copy 30 m) is a reset and adds the
    reappearing value v; a shorter gap adds v − last, or v when v < last.
    *(Corrected 2026-11-23, K-count code review H1: the expiry is decided
    by the time since the value last changed, not by the sample gap — the
    exporters keep scraping a flat value until the expiry, then the series
    vanishes and comes back about one scrape later. Rule: when the sample
    gap is more than 2 × the scrape interval and the reappearance is at
    least expiry − one scrape after the last change, it is an expiry and
    adds v; otherwise the sample adds v − last, or v when v < last. Scrape
    interval 30 s, an initial value (`unmeasured`).)*
    *(Corrected 2026-11-23, K-count re-review HIGH-1: a reappearance one or
    two scrapes after the expiry cannot be told from a real update just
    before it. The band — sample gap ≤ 2 × scrape, value unchanged for at
    least expiry − one scrape, v ≥ last — is ambiguous: the lower count adds
    v − last, the upper count adds v, both are carried into the bound (LOSS
    only when the upper count is below it, DUPLICATES only when the lower
    count is above it), and the event "ambiguous: expiry or update" is
    recorded. The scrape interval is per source: measured 2026-11-23 on
    k8s01 as 15 s for the copy (`count_over_time(up{job="dev-otel-libs/soak-agent",
    endpoint="count"}[1h])` = 240) and 30 s for the gateway span metrics
    (120 samples per hour); each read uses its own median sample spacing when
    it has at least ten spacings, else these values.)*
  - *Cluster scope (H5)*: every query on a shared job carries
    `cluster="k8s01"`.
  - *FD sweep (L2)*: the exec read runs at every 60 s guard read: 12 demo
    containers → 12 exec calls per minute, 720 per hour, about 21 600 over
    the 30 h K8a window.
  - *Label no-op (L3)*: the guarded namespace-label restore is a K-loop test
    case (label `ambient` → no write; any other value → one `--overwrite`).
- **Deferred in r6.2 (2026-11-23).** K4 (Mesh), K7 (Health), K8c
  (multi-worker Python), the K10 burst mode and the per-`severity_number`
  log counts are deferred, AG10, AG9, AG8, AG7 and AG6; their bullets above
  stay as written and their promotion triggers are in tasks.md.

## Acceptance (for `0.3.0` final, D10)

> *Superseded 2026-11-19 (r6): the r4 list below assumed the Compose full run
> on a released rc. Phase S is dropped and the artifact is the branch HEAD
> (owner rulings 1 and 3); the Phase K list under it replaces this one.*

- [ ] ~~A full run on the **last rc** passes SR4–SR10, SR12, SR13 (SR11 report
      produced).~~
- [ ] ~~Every failure is fixed in the libs (new rc) or recorded as an accepted
      gap with an owner ruling; the `0.3.0` commit differs from the soaked rc
      only in version strings and CHANGELOG (diff checked).~~
- [ ] ~~Report refuted by `observability` + `dev` agents; linked from D10 and
      the CHANGELOG.~~

### Acceptance in Phase K terms (r6.2, lean soak, 2026-11-23)
- [ ] K1: the snapshot passes, including the runtime-metric names, the
      `exported_instance` label on the copy series, the 1 h floor reading and
      the Prometheus PVC record.
- [ ] K-loop: a dry run creates one soak-run object of each kind and the trap
      deletes exactly those; the unit tests pass.
- [ ] K-copy: the positive control through the copy reads within its bound
      (N sent, N counted, accepted = sent) and K2 shows the demo pods' (not
      the copy's) `k8s.pod.name`.
- [ ] K8a: the coverage gate passed and a valid passive window (30 h: 12 h
      warm-up + 18 h judged) passes "Soak rules (K8a)" with the S-D5 2026-11-23
      recalibration (FDs in all four languages by the named `exec` read;
      Node.js runtime state not judged, AG12), `verify.py demo` passes at its start and end, and the
      "Mesh and platform guard" stayed clean.
- [ ] K-cal: the fixed-family k values are integers and stable across two
      repeats, or the failing families are excluded and named; the drain
      rate and the loaded rate are recorded.
- [ ] K8b: a valid 2 h loaded window has no LOSS and no DUPLICATES on the
      fixed span families ("Counting in bounds"), clean "Sentinels (r6.2)",
      a passing drain check, the api → backend service-graph share, the
      secret canary, the SR9 orphan check on the k6 reservoir and the SR10
      comparison (only documented gaps differ). Every other count is in the
      summary, report-only.
- [ ] K-shut, K3 and K5 pass ("Shutdown (r6.2)", "Restart mechanics (r6.2)").
- [ ] K10 (refused, hung, back-pressure, redirect) and K10b: every mode passes
      or is reported `not injected` with the reason observed.
- [ ] Every failure is fixed in the libs and re-run on the new HEAD, or is an
      owner-ruled accepted gap (AG1–AG12).
- [ ] The code freeze held (no change under `go/`, `python/`,
      `nodejs/packages/`, `dotnet/` since the local tag `soak/<run>`), and the `0.3.0`
      commit differs from the soaked commit only in version strings,
      `CHANGELOG.md` and documentation.
- [ ] The summary `reports/<run>.md` passed the secret scan, is committed,
      refuted by `observability` + `dev`, and linked from D10 and the
      CHANGELOG.

### Acceptance in Phase K terms (r6, 2026-11-19)

> *Superseded 2026-11-23 (r6.2, lean soak) by the list above: exact counts
> become bounds on the fixed span families; the r6.1 AG6–AG10 item is
> withdrawn (those gaps are accepted); the black hole leaves K10.*
- [ ] K-copy: the positive control reads exactly N through the copy for
      spans, log records and data points, and K2 shows the demo pods' (not
      the copy's) `k8s.pod.name` behind it.
- [ ] K8a: a valid passive window ≥ 24 h (all four languages together, or
      every rotation window) passes "Soak rules (K8a)", with `verify.py demo`
      passing at its start and end.
- [ ] K8b + K6: a valid 2 h loaded window passes "Counting" (spans and log
      records per service: no LOSS, no DUPLICATES), "Sentinels", the
      api → backend service-graph share, the secret canary, the SR9 orphan
      check on a trace sample and the SR10 cross-language comparison (only
      documented gaps differ).
- [ ] K-shut, K3, K5 at the end of the loaded window pass.
- [ ] K10 subset and K10b: every mode passes or is reported `not injected`
      with the reason observed.
- [ ] *(r6.1, 2026-11-20)* K8b per-severity record counts (AG6), the K10
      burst mode (AG7), K8c multi-worker Python (AG8), K7 (AG9) and K4 (AG10)
      pass.
- [ ] Every failure is fixed in the libs and re-run on the new HEAD, or
      recorded in the accepted-gap list below with an owner ruling; the list
      is signed by the owner (task K0d). *(r6.1, 2026-11-20: signed — AG1–AG5.)*
- [ ] The `0.3.0` commit differs from the soaked commit only in version
      strings, `CHANGELOG.md` and documentation (`*.md`, `docs/site/`) — diff
      checked; any other change needs a new loaded window or an owner ruling.
- [ ] The run summary (`reports/<run>.md`) is committed, refuted by
      `observability` + `dev`, and linked from D10 and the CHANGELOG.

### Coverage of SR0–SR15 (r6.2, lean soak, 2026-11-23)

| SR | Phase K status |
|---|---|
| SR0 | K-cal, K8a, K8b — demo apps, four languages; per-severity records → AG6 |
| SR1 | K-copy (agent-only copy, plaintext); gateway `tail_sampling` / `loadbalancingexporter` → AG2; TLS → AG1; VictoriaMetrics → AG4 |
| SR2 | K8a / K8b (`dev`, OTLP/gRPC); OTLP/HTTP in K10 *redirect*; other environments → AG5; TLS → AG1 |
| SR3 | K8b ("Load", "Load (r6.2)") |
| SR4 | K8a (leaks, threads, FDs in four languages by `exec`, goroutines, .NET thread pool, restarts; Node.js runtime → AG12); K8b (fixed-family bounds, sentinels, drain check) |
| SR5 | K10 refused, hung, back-pressure; black hole → AG11 |
| SR6 | K10b (DNS name at start-up, gRPC to 4318, no endpoint → `/metrics`); wrong CA → AG1 |
| SR7 | K-shut ("Shutdown (r6.2)") |
| SR8 | inbound sampled=00 → K8b (0 fixed-family spans for that class); sampling shares, `prd-debug`, `dev-debug` → AG2; per-environment log levels → AG5; health-span absence → AG9 |
| SR9 | orphan check → K8b k6 reservoir; `gateway.id` routing, gateway-1 restart → AG2 |
| SR10 | K8b (four languages, Prometheus and Tempo); VictoriaMetrics → AG4 |
| SR11 | `n/a: dropped (owner 2026-11-19)` |
| SR12 | K3 |
| SR13 | back-pressure → K10; Prometheus fallback → K10b; backend outage → AG3; burst → AG7; multi-worker Python → AG8 |
| SR14 | `n/a: dropped (owner 2026-11-19)`; its safety rules are SR15 "Orchestration (r6.2)" and "Evidence (r6.2)" |
| SR15 | K1, K-loop, K-hang, K-copy, K8a, K-cal, K8b, K-shut + K3 + K5, K10, K10b, K11; mesh and platform guard in K-loop; K4, K7, K8c, K9 deferred |

### Coverage of SR0–SR15 (r6, 2026-11-19)

> *Superseded 2026-11-23 (r6.2) by the table above.*

| SR | Phase K status |
|---|---|
| SR0 | K-cal, K8a, K8b — demo apps, four languages (SR0 amendment r6); per-severity records → K8b (AG6 not accepted) |
| SR1 | K-copy (agent-only copy, plaintext); gateway `tail_sampling` / `loadbalancingexporter` → AG2; TLS → AG1; VictoriaMetrics flags → AG4 |
| SR2 | K8a/K8b (`dev`, OTLP/gRPC); OTLP/HTTP in K10 *redirect*; `hml` / `prd` / `unset` / `prd-debug` sets → AG5; TLS → AG1 |
| SR3 | K8b (k6 "Load" bullet) |
| SR4 | K8a (leaks, restarts), K8b + K6 (completeness, loss accounting through the copy) |
| SR5 | K10 ("Failure modes") |
| SR6 | K10b (wrong DNS name at start-up — the only DNS case, gRPC to 4318); wrong CA → AG1 |
| SR7 | K-shut |
| SR8 | inbound sampled=00 → K8b (0 spans per class, calibrated); sampling shares, `prd-debug` bound, `dev-debug` → AG2; per-environment log levels → AG5; health span absence → K7 (AG9 not accepted) |
| SR9 | orphan check → K8b trace sample; `gateway.id` routing, gateway-1 restart → AG2 |
| SR10 | K8b (four languages, Prometheus and Tempo sets; VictoriaMetrics → AG4) |
| SR11 | `n/a: dropped (owner 2026-11-19)` — task S5 was Phase S; consumer evidence, never a gate |
| SR12 | K3 |
| SR13 | agent back-pressure → K10 *back-pressure* (through the copy); Prometheus fallback → K10b; backend outage → AG3; burst with a hung peer → K10 *burst* mode (AG7 not accepted); multi-worker Python → K8c (AG8 not accepted) |
| SR14 | `n/a: dropped (owner 2026-11-19)` — Compose orchestration; its safety rules are SR15 "Orchestration" and "Evidence" |
| SR15 | K1, K-copy, K7, K8a, K-cal, K8b + K6, K8c, K-shut, K3, K5, K10, K10b, K4, K11; K9 deferred |

### Accepted gaps (for the owner to sign — task K0d)

> *Signed 2026-11-20 (owner, K0d):* **AG1, AG2, AG3, AG4, AG5 accepted.**
> AG6–AG10 **not accepted**; each keeps its id with the task that covers it.
>
> *Ruled 2026-11-23 (owner, lean soak, tasks.md K0d2):* **AG6, AG7, AG8, AG9,
> AG10 accepted** — the cost of the lean scope, superseding the 2026-11-20
> "not accepted" — and a new **AG11** accepted. *(K0e, 2026-11-23: plus
> **AG12** no Node.js runtime metrics, accepted.)* The final list is AG1–AG12.
> The tasks that would close AG6–AG10 stay as deferred rows with promotion
> triggers (tasks.md).

Owner-listed on 2026-11-19 (rulings 5 and the BLOCK resolution):
- **AG1 TLS / private CA** — the copy is plaintext (owner). Not checked:
  SR1/SR2 TLS set, SR6 wrong CA, `OTEL_EXPORTER_OTLP_CERTIFICATE`. *Accepted 2026-11-20 (owner).*
- **AG2 Gateway tail sampling and trace-ID load balancing** — the copy is
  agent-only and the platform gateway runs neither. Not checked: SR1 gateway
  processors, SR8 sampling formulas, `prd-debug` bound and `dev-debug`, SR9
  `gateway.id` routing and the gateway-1 restart, S-D8, D11.1–D11.3. *Accepted 2026-11-20 (owner).*
- **AG3 Backend outage** — Tempo, Loki and Prometheus are shared (SR13). *Accepted 2026-11-20 (owner).*
- **AG4 VictoriaMetrics** — the homelab Prometheus is the reference (SR1
  flags, SR10 naming in VictoriaMetrics, D11.6's VictoriaMetrics flags). *Accepted 2026-11-20 (owner).*

Proposed in r6 (not in the owner's list; each needs a signature or a task):
- **AG5 Environment matrix** — the demo runs `deployment.environment.name=dev`
  only: no `hml` / `prd` / `unset` / `prd-debug` sets, so SR2's matrix and
  SR8's per-environment log levels are left to the per-request parity cases.
  *Accepted 2026-11-20 (owner).*
- **AG6 Per-severity log records** (SR0) — the demo writes INFO and ERROR.
  *Not accepted 2026-11-20 (owner): covered by K8b (per-`severity_number`
  Loki counts per app against expected).*
  *Accepted 2026-11-23 (owner, lean soak): every log count is report-only;
  K8b records the per-severity counts without a verdict.*
- **AG7 Burst over the SDK queue with a hung peer** (SR13) — the K10 *hung*
  mode runs at the K-cal rate and may not fill the 2048-item queue.
  *Not accepted 2026-11-20 (owner): covered by K10 (burst-with-hung-peer
  mode on the hung sink).*
  *Accepted 2026-11-23 (owner, lean soak): the burst mode is deferred.*
- **AG8 Multi-worker Python** (SR13, D4 fork path) — the demo's Python apps run
  one process. *Not accepted 2026-11-20 (owner): covered by K8c (with demo
  task DE14).*
  *Accepted 2026-11-23 (owner, lean soak): K8c deferred, DE14 dropped, no
  gunicorn dependency.*
- **AG9 Health-span absence app-side** (SR8 health, K7 deferred) — the agent's
  `filter/noise` hides it; parity cases cover the helper filter per request.
  *Not accepted 2026-11-20 (owner): covered by K7 (promoted from deferred).*
  *Accepted 2026-11-23 (owner, lean soak): K7 deferred.*
- **AG10 Mesh toggle** (K4 deferred, D11.5) — every result carries the ambient
  condition (no waypoint, no mesh tracing, measured 2026-11-15). *Not
  accepted 2026-11-20 (owner): covered by K4 (promoted from deferred; demo
  scaled to 0 for its window).*
  *Accepted 2026-11-23 (owner, lean soak): K4 deferred; every result carries
  the ambient condition, and the "Mesh and platform guard" makes a broken
  mesh an invalid window, not a result.*
- **AG11 NetworkPolicy black hole** (SR5, added 2026-11-23) — the
  packets-dropped-no-RST mode on the app → copy hop. Under ambient a
  namespaced egress NetworkPolicy may not be enforced (r6.1 recorded that
  case as `not injected`); the lean soak drops the mode instead. Not
  checked: an exporter facing a peer that never answers at the TCP level
  (the *hung* mode covers a peer that answers TCP and HTTP/2 and then
  blocks). *Accepted 2026-11-23 (owner, lean soak).*
- **AG12 No Node.js runtime metrics** (SR4, added 2026-11-23 from the
  pre-soak analysis) — the Node.js demo services export no `v8js_*` or
  `nodejs_*` series to Prometheus, so their heap, event loop and handles are
  not judged; container RSS, working set, threads, FDs (named `exec` read)
  and restarts are. *Accepted 2026-11-23 (owner, ruling K0e-2, as part of the
  lean scope).* *(The first r6.2 draft proposed AG12 for FDs outside Python
  too; the owner's FD `exec` ruling removed that part.)*

## Out of scope
- Real traffic shapes/volumes and Collector capacity sizing.
- .NET (0.2.x until Phase C) and Node.js (Phase B).
  *(Amended 2026-11-19, r6: all four languages are in scope (owner ruling 2):
  Node.js and .NET have implemented the 0.3 contract on this branch, and the
  demo apps exist in all four.)*
- Kubernetes-specific behavior — Phase K (tasks.md): `k8sattributes`, pod
  identity recipe (D11.4), chart wiring, Istio (D11.5), platform chart values.
  *(Amended 2026-11-15, r5: K2–K5 are now in scope on `k8s01` — SR15. Still
  out of scope:)*
- *(Amended 2026-11-19, r6: the copy `soak-agent` is deployed by the
  platform owner's GitOps session on request (owner ruling 5) — an owner
  change, not a Phase K write. The rule below still binds the run.)*
- Changing the shared platform: Collector CRs, Prometheus/Tempo/Loki
  configuration, Istio mesh config (waypoints, tracing providers,
  `Telemetry`), Kyverno policies, OBI configuration, PriorityClasses. A check
  that needs one of them is reported `n/a: platform` with the change it would
  need. (Kubelet `maxPods` is no longer listed: the owner raised it,
  K0b ruling 3 — an owner change, not a Phase K write.)
- Tail sampling, trace-ID load balancing and their D11.1–D11.3 items on the
  cluster (no such gateway there, K-D1).
  *(r6, 2026-11-19: accepted gap AG2.)*
- Using Phase K results as D10 acceptance evidence before the owner rules it
  (K0b); Phase K is additional evidence.
  *(Amended 2026-11-19, r6: ruled — Phase K is the D10 evidence, with the
  accepted gaps above; config-contract D10 amendment of the same date.)*
- OBI as a supported deployment mode of the helpers (K9 is a one-off
  measurement).
