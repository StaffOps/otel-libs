# Soak simulation — tasks

**Status**: Draft r6.2 "lean soak" (2026-11-23, owner ruling K0d2 after two
K0c reviews BLOCKED r6.1; not yet reviewed — K0e). History: r6.1 (2026-11-20, owner rulings on r6 — K0d); r6 (2026-11-19; r4 2026-10-28; Phase K r5 2026-11-15
BLOCKED by two K0 reviews; r6 not yet reviewed — K0c). Phase S dropped
(owner, 2026-11-19); Phase K on `k8s01` carries the D10 evidence. Spec: [requirements](requirements.md) ·
[design](design.md). Feeds: config-contract-v0.3 D10 (rc soak) → `0.3.0` final.

## Phase S — Docker Compose simulation (now)

> *Dropped 2026-11-19 (owner ruling 1, K0b2).* Only Phase K runs. The rows
> are kept for the record; what only this phase could show is an accepted
> gap (requirements.md, "Accepted gaps").

| Task | Depends on | Covers | Status |
|---|---|---|---|
| S0 Adversarial review r1 (`observability` + `dev`) | — | format rule | ✅ 2026-10-28 (r2 written) |
| S0b Re-review r2 (`observability` + `dev`) | S0 | format rule | ✅ 2026-10-28 (r3 written) |
| S0c Re-review r3 (dev: clear; observability: 2 HIGH) | S0b | format rule | ✅ 2026-10-28 (r4 written) |
| S0d Re-review r4 (observability) | S0c | format rule | ✅ 2026-10-28 (no BLOCKING/HIGH; last MEDIUM/LOWs applied) |
| S1 Stack: agent (+ `agent-net` holder), 2 gateways, Tempo, Loki, VictoriaMetrics, Grafana, private CA (reuse fakeotlp cert mode), pinned versions, internal telemetry readers, dedicated buildx builder | S0d | SR1, S-D2, S-D7, S-D8, S-D12 | Dropped 2026-11-19 (owner) |
| S2 Soak apps Go + Python (SR0 contract) + Dockerfiles in released / `--from-tree` modes; **build check of the 0.2 apps first** (Go 0.2.1 deps on `golang:1.26`; if it fails, a `golang:1.22` builder enters the disk budget) | S0d | SR0, S-D9, S-D10 | Dropped 2026-11-19 (owner) |
| S3 k6 open-loop load script (route mix, inbound traceparent 00/01, client-side log) | S2 | SR3 | Dropped 2026-11-19 (owner) |
| S4a Checker: count pipelines + **positive control** (telemetrygen), completeness + loss accounting | S1–S3 | SR4, S-D6 | Dropped 2026-11-19 (owner) |
| S4b Failure modes refused / black hole / hung / DNS | S4a | SR5, S-D12 | Dropped 2026-11-19 (owner) |
| S4c Misconfiguration cases (separate phase) | S4a | SR6 | Dropped 2026-11-19 (owner) |
| S4d Shutdown | S4a | SR7 | Dropped 2026-11-19 (owner) |
| S4e Environment behavior: logs, sampling formulas, prd-debug bounds, dev-debug phase, traceparent 00 phase, health at agent | S4a | SR8, S-D8 | Dropped 2026-11-19 (owner) |
| S4f Trace integrity + gateway-1 restart | S4a | SR9 | Dropped 2026-11-19 (owner) |
| S4g Parity sets | S4a | SR10 | Dropped 2026-11-19 (owner) |
| S4h Restart identity | S4a | SR12 | Dropped 2026-11-19 (owner) |
| S4i Burst, backend outage, back-pressure, multi-worker, Prometheus fallback | S4a | SR13 | Dropped 2026-11-19 (owner) |
| S5 0.2 apps (own source, separate go.mod, constraints, always-sample path; build them first) + migration query set (separate phase) | S4a | SR11, S-D9 | Dropped 2026-11-19 (owner) |
| S6 `run.sh` (phases, timeline, stats sampling, pre-flight, disk monitor, sleep guard, trap, purge) + report writer | S4a | SR14, S-D3, S-D4, S-D7 | Dropped 2026-11-19 (owner) |
| S7 Calibration + short run on `0.3.0-rc.2`: record mem_limit, ingest rate, p99 trace duration + batch delays → `decision_wait`, spans per route, max spans per trace (→ R), resume bounds, durations; fix the stack until trustworthy | S6 | S-D3, S-D5, S-D8 | Dropped 2026-11-19 (owner) |
| S8 Full run on the last rc; fix lib defects (new rc), re-run until it passes | S7 | Acceptance | Dropped 2026-11-19 (owner) |
| S9 Report refutation (`observability` + `dev`), `code-review` of the stack | S8 | change loop | Dropped 2026-11-19 (owner) |
| S10 Record result in D10 + CHANGELOG; `testing/soak/README.md`; README gaps list (runtime metric families) | S9 | change loop, SR10 | Dropped 2026-11-19 (owner) |

## Phase K — Kubernetes (`kind`) — **deferred (owner, 2026-10-28)**

Not executed now. Same checker (S-D4) and scenarios, plus K8s-only checks.

> *Amended 2026-11-15 (r5): promoted onto the homelab cluster — see "Phase K
> on `k8s01`" below. This table and its trigger are kept as written on
> 2026-10-28; its "same checker (S-D4)" premise does not hold (S4a is not
> built, and the cluster has no tail-sampling gateway — design K-D1).*

| Task | Covers | Status |
|---|---|---|
| K1 `kind` + Collector agent DaemonSet / gateway Deployment via the platform charts (or faithful copies) | D11.3, D11.4 | Superseded 2026-11-15 (r5 table below) |
| K2 `k8sattributes` enrichment visible in all backends; apps set only `service.name`/`deployment.environment.name` | AGENTS.md principle 4 | Superseded 2026-11-15 (r5 table below) |
| K3 Pod identity: semconv K8s recipe for single-process pods; no series duplication on rollout | D4, D11.4 | Superseded 2026-11-15 (r5 table below) |
| K4 Istio ambient/sidecar tracing at 100% vs disabled under ParentBased | D11.5, ADR-002 | Superseded 2026-11-15 (r5 table below) |
| K5 VictoriaMetrics label set with real pod attributes, rollout restarts | D11.6 | Superseded 2026-11-15 (r5 table below) |

**Promotion trigger**: any of — a cluster (kind or real) is available and
Phase S passed; a Phase S result that cannot be explained without K8s; the
platform team (A12) asks for chart-level evidence; before `0.3.0` reaches a
Kubernetes PRD rollout.

## Phase K on `k8s01` — **promoted 2026-11-15 (r5 draft; owner confirmation K0b)**

> **Promotion note (2026-11-15).** A real cluster is available: kube context
> `k8s01`, namespace `dev-otel-libs` (kubectl-direct, owner decision),
> tooling in `testing/k8s/` (commit `dc4d08e`). The trigger above is only half
> met: its first clause reads "a cluster … is available **and** Phase S
> passed", and Phase S has not run (S1–S10 ☐). r5 therefore promotes the
> checks that need no Phase S evidence (K1–K7, K9), keeps the soak-length ones
> (K8, K10) behind the soak apps (S2, S3), and asks the owner to rule the
> trigger amendment (K0b). Measured 2026-11-15 with read-only `kubectl get` /
> `get --raw` (details and commands in design K-D1…K-D9):
> - the platform pipeline is shared and ArgoCD-managed: OpenTelemetry operator
>   `0.162.0`, Collector contrib `0.165.0` (namespace `opentelemetry`):
>   `otel-agent` DaemonSet (`memory_limiter` → `k8s_attributes` → `resource`
>   (`k8s.cluster.name`) → traces only `filter/noise` → `batch`) → one
>   `otel-gateway` replica (`memory_limiter` → `batch`; connectors
>   `span_metrics`, `service_graph`; an external `:4319` receiver with bearer
>   auth) → Tempo, Loki `3.7.10`, Prometheus `3.16.0` (OTLP receiver) — not
>   VictoriaMetrics. A third Collector, `otel-logs` (DaemonSet, `file_log` on
>   `/var/log/pods/*`), also ships every container's stdout to Loki;
> - the gateway has **no `tail_sampling` and no `loadbalancingexporter`**:
>   SR8 sampling shares, SR9 trace-ID routing, S-D8 and D11.1–D11.3 cannot be
>   checked here (K-D1);
> - `dev-otel-libs` **is already in Istio ambient** (`istio.io/dataplane-mode=
>   ambient`, set 2026-11-15T16:21:21Z by Kyverno MutatingPolicy
>   `namespace-ambient-mode`; pods annotated `ambient.istio.io/redirection=
>   enabled`), like every namespace except `kube-*`, `istio-system` and the
>   two Calico ones; Istio `1.30.8`, no waypoint, no tracing provider, no
>   `Telemetry` and no `PeerAuthentication` objects (K-D5);
> - pod slots: node `allocatable.pods` 110; at 19:31 UTC 110 pods Running and
>   2 Pending — a Velero maintenance Job (`FailedScheduling: 0/1 nodes are
>   available: 1 Too many pods`) and a `dev-otel-libs` pod. Free slots at that
>   moment: 0 (K-D2). *(r5 revision, 2026-11-15, after the K0 review: the
>   owner then raised kubelet `maxPods` (K0b ruling 3) — `allocatable.pods`
>   200, 117 non-terminated pods. The binding constraint is now requests:
>   CPU 7395 m of 8 (≈ 600 m free), memory 27008 Mi of ≈ 31.2 Gi (≈ 5 Gi
>   free); re-read later the same day 7595 m / 27840 Mi. K-D2 budgets
>   requests, keeps the slot check as a guard.)*;
> - Kyverno ValidatingPolicies (restricted-style) run in `Audit`, not
>   enforce (all twelve, `spec.validationActions`).

| Task | Depends on | Covers | Status |
|---|---|---|---|
| K0 Adversarial review of r5 (`observability` + `dev`) | — | format rule | ✅ 2026-11-19 — two independent reviews BLOCKED r5 (stale facts, no platform guards, inexact K6 counting, failure modes without criteria, Acceptance and D10 not in Phase K terms); applied in r6 |
| K0b Owner rulings: trigger amendment; slot budget and platform reserve; ambient toggle on `dev-otel-libs`; OBI namespace; reference platform for Phase S (homelab Prometheus or the A12 VictoriaMetrics) | K0 | K-D1, K-D2, K-D5, K-D7, K-D9 | ☐ *Owner rulings 2026-11-15:* (1) trigger amended — a cluster being available is enough to run Phase K now, as additional evidence; soak acceptance (D10) still needs Phase S, which covers the gateway tail sampling and load balancing this cluster lacks; (2) the K4 ambient toggle is allowed: `istio.io/dataplane-mode=none` on `dev-otel-libs` only for the K4 window, restored by the run trap (Kyverno `namespace-ambient-mode` only acts when the label is missing); (3) slot budget: the owner raised the kubelet `maxPods` (value recorded in the K1 report) instead of a PriorityClass or a script reserve; the script pre-flight check stays as a guard. Open: OBI namespace (K9), the Phase S reference platform (K5), `k8s.container.name` in multi-container pods, K10 failure modes under ambient. |
| K0b2 Owner rulings 2026-11-19 (after the two K0 BLOCKs) | K0 | requirements r6, K-D10…K-D16, config-contract D10 | ✅ 2026-11-19 (recorded below) |
| K1 Use the existing platform pipeline (replaces `kind`): `verify.py platform` records the three Collector CRs (credentials stripped), Prometheus `otlp` settings, backend versions, the namespace ambient label, every Gateway API `Gateway` and the node's allocatable / requested CPU, memory and pods in the report, and fails when a component a K check relies on is missing or changed. New code in `verify.py`: a Prometheus `query_range` client and a read-only `kubectl get -o json` wrapper (it has neither today, K-D3) | K0b | D11.3, D11.4, SR15, K-D1, K-D3 | ◐ smoke + examples ran 2026-11-15 (`testing/k8s/README.md` "Measured results"); snapshot mode not built *Superseded 2026-11-19 (r6 table below).* |
| K2 `k8s_attributes` enrichment asserted in Tempo, Loki and Prometheus; the apps set only `service.name` / `deployment.environment.name` (+ the `otel_libs.run` test tag); `k8s.container.name` asserted in single-container pods only (K-D4) | K1 | principle 4, SR15 | ◐ Tempo 8/8 smoke resources (`verify.py smoke --run r1`); Loki and Prometheus not asserted per service; multi-container pods lack `k8s.container.name` *Superseded 2026-11-19 (r6 table below).* |
| K3 Pod identity: the helper's `service.instance.id` reaches every backend unchanged (the agent does not extract it); new id per process after `rollout restart`; old `instance` has no sample later than the old container's `terminated.finishedAt` + one export interval, sample time read with `max_over_time(timestamp(…)[w:<step>])` or a raw range selector, not `timestamp(last_over_time(…))` (SR12, SR15); semconv K8s recipe variant via the Downward API in one single-process Deployment | K1 | D4, D11.4, SR12, SR15 | ☐ *Superseded 2026-11-19 (r6 table below).* |
| K4 Istio ambient (ztunnel L4, no waypoint) vs `istio.io/dataplane-mode=none` on `dev-otel-libs` only, `split` layout so the api → backend hop crosses ztunnel: same spans per route, same parent links, enrichment intact, no mesh-generated span; label restored by the run trap | K0b, K1 | D11.5, ADR-002, K-D5 | ☐ *Superseded 2026-11-19 (r6 table below).* |
| K5 Prometheus label set (replaces the VictoriaMetrics row): `job` = `<service.namespace>/<service.name>`, `instance` = `service.instance.id`, the eleven promoted attributes; `deployment.environment.name` only on `target_info` (join); series count per rollout restart | K1 | D11.6 (Prometheus variant), SR10, SR12, K-D9 | ☐ *Superseded 2026-11-19 (r6 table below).* |
| K6 Gateway connectors as soak signals: raw `traces_span_metrics_calls_total` per `job` × `span_name` at two quiesced checkpoints (t2 − t1; a series absent at t1 counts its t2 value; a reset invalidates the window) vs load-generator answered requests × calibrated spans per route, exact; `traces_service_graph_request_{total,failed_total}` per edge vs the load generator's 5xx share; `otelcol_connector_servicegraph_expired_edges` reported | K1, S3 | SR4 (cluster variant), SR15 | ☐ *Superseded 2026-11-19 (r6 table below).* |
| K7 Health-span absence proven app-side: the agent's `filter/noise` drops `/health` (and `/metrics`, `/ready`, …) before every backend, so absence in Tempo proves nothing about the helper (K-D6) | K1 | D5, SR8 (health) | ☐ *Superseded 2026-11-19 (r6 table below).* |
| K8 Cluster steady soak on the S2 soak apps: one language per window, `split` layout (load generator in its own pod), every container with explicit requests that pass the K-D2 pre-flight, languages rotated unless the budget fits more (K-D2); Loki records counted by `service_instance_id` across `__stream_shard__` streams; S-D5 leak rules on cAdvisor `container_memory_rss` / `container_threads`; restarts from `kube_pod_container_status_restarts_total`; FDs `n/a` (no source, K-D3) | K0b, K2, S2, S3 | SR4, S-D5, SR15 | ☐ *Superseded 2026-11-19 (r6 table below).* |
| K9 OBI vs SDK, opt-in experiment in an owner-created `staffops*` namespace: OBI's rule `exclude-otlp-exporters` (OTLP/protobuf to port 4317) keeps OBI off helper processes — no duplicate spans; which helper transports it matches is `unmeasured`; optional uninstrumented twin for a span-shape comparison (K-D7) | K0b, K1 | principles 1, 7 (evidence only) | ☐ opt-in, not an acceptance gate *Superseded 2026-11-19 (r6 table below).* |
| K10 Failure modes without touching shared Collectors (K-D8): DNS failure (endpoint Service name that does not exist), refused/timeout (Service with no endpoints — behavior under ambient `unmeasured`), black hole (namespaced egress NetworkPolicy — effect under ambient `unmeasured`) | K0b, K8 | SR5 subset | ☐ *Superseded 2026-11-19 (r6 table below).* |
| K11 Report and refutation: summary `reports/<run>.md` committed (flags every mid-window platform roll); refutation by `observability` + `dev` (scored per language and check); `code-review` of `verify.py soak` and the run loop; D10 and CHANGELOG link the summary; `testing/k8s/README.md` updated | K4, K8c | Acceptance, change loop | ☐ |

**Not on this cluster** (stay in Phase S, or a future cluster whose gateway
runs `tail_sampling` behind a `loadbalancingexporter`): SR8 sampling shares
and the `prd-debug` bound, SR9, S-D8, D11.1–D11.3; SR5 modes that stop or
pause the agent; SR13 backend outage, agent back-pressure and burst-with-hung-
agent; SR14 Compose isolation (replaced by SR15 scope rules).

**Promotion trigger for the rows above that are not here**: the platform
gateway gains `tail_sampling` + `loadbalancingexporter`, or the owner grants
CPU and memory headroom (K-D2 budget) for a namespaced agent + gateway copy
(K-D1 reversal). *(r5 revision, 2026-11-15: was "≥ 2 reserved slots"; slots
stopped being the constraint after the `maxPods` raise.)*
*(Amended 2026-11-19, r6: met for the agent half — `soak-agent` runs in
`dev-otel-libs` (K-D10) and the r6 table carries the SR5 modes, back-pressure
and loss accounting it enables; the gateway half (tail sampling, load
balancing) and the backend outage stay out, accepted gaps AG2 and AG3.)*

## Owner rulings 2026-11-19 (K0b2)

1. Only Phase K runs, on `k8s01`; Phase S is dropped (S1–S10 marked, rows
   kept).
2. Workload: the existing demo apps in all four languages, driven by k6 in a
   pod (open loop, `constant-arrival-rate`, `dropped_iterations` = 0,
   `handleSummary` JSON with counters per route × status × inbound class,
   `activeDeadlineSeconds`, image by digest from docker.io) on `POST /orders`
   with bounded ids; the worker stays as the queue consumer with
   `DEMO_INTERVAL_SECONDS=3600` (*changed 2026-11-20 to 10800, ruling K0d-2*); an env-override hook in `demo.sh` through a
   dated demo-environment amendment (DE-D7, task DE13).
3. Artifact: the branch HEAD — demo images built from the commit under test,
   digests recorded; a dated deviation from S-D9 (no rc cut this round).
4. Duration: 24 h (30 h, K0e-1) passive at the demo's own rate, plus one 2 h loaded window
   at a calibrated rate; all languages together if the pre-flight fits,
   rotation only if not.
5. A dedicated Collector copy in `dev-otel-libs` (reverses K-D1), deployed by
   the platform owner's GitOps session, no TLS. *Delivered 2026-11-19 as
   `soak-agent` (design K-D10).* TLS / private CA is an accepted gap.
6. Platform guards: refused / failed / enqueue-failed at the shared agent or
   gateway, `memory_limiter` refusals, Tempo / Loki discards, node real use or
   root filesystem below a floor → k6 to 0, window invalid; floors start as
   dated `unmeasured` values; an absent error counter is 0 only with a sibling
   success metric of the same job present.
7. Evidence: a committed summary per run under `reports/<run>.md`; raw output
   git-ignored.
8. Orchestration on the operator's laptop: `caffeinate`, a wall-clock gap
   invalidates, a blind look-up stops the load, a trap deletes only
   run-labelled objects.

## Phase K r6 — minimal path (added 2026-11-19)

Ordered; each row is verifiable by its "done when". Requirement bullets are
SR15's r6 bullets; decisions K-D10…K-D16.

| Task | Depends on | Covers | Status |
|---|---|---|---|
| K0c Adversarial review of r6: two independent reviewers (`observability` + `spec-refuter` or `dev`), every claim with file:line, command output or upstream source | K0b2 | format rule | ✅ 2026-11-23 — two independent reviews BLOCKED r6.1 (no task for the run loop, no mesh guard, exact counts that cannot hold, worker drain and first drive, K-shut overlap, hung sink, sentinel names) and estimated 19–28 days; owner ruled the lean soak (K0d2), applied in r6.2 |
| K0d Owner signs the accepted-gap list (AG1–AG4 owner-listed; AG5–AG10 proposed) and answers the r6 open questions | K0c | Acceptance | ✅ 2026-11-20 (owner, rulings below): AG1–AG5 accepted; AG6–AG10 not accepted, covered by K8b, K10, K8c, K7, K4. *Corrected 2026-11-23 (r6.2): K0d ran on r6 before K0c had reviewed it, against its own dependency; its AG1–AG5 rulings stand, its AG6–AG10 part is superseded by K0d2, which follows K0c.* |
| DE13 (demo-environment) env-override hook in `demo.sh` — see that spec's tasks | K0c | SR15 Windows, K-D11 | ☐ *Amended 2026-11-23 (r6.2): also `DEMO_RESTART_NONCE`, `demo.sh scale`, deny `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` — r6.2 table.* |
| DE14 (demo-environment) Python api multi-worker mode for K8c — see that spec's tasks (added 2026-11-20) | K0d | SR15 Multi-worker Python (K8c) | Dropped 2026-11-23 (lean soak) — AG8 accepted, no gunicorn dependency |
| K1 Platform snapshot, lite: `verify.py platform` records node allocatable / requests / pods, Prometheus `spec.otlp` (the promoted list, `translationStrategy`), backend and Collector versions and digests (read 2026-11-19: Prometheus 3.16.0, Tempo 3.1.1, Loki 3.7.10, contrib 0.165.0, operator 0.162.0), the shared CRs and the `soak-agent` ConfigMap (credentials stripped), the gateway pod's name and start time, the namespace ambient label, and the exposed names of the K8a runtime metrics (Go goroutines, .NET thread pool, Python FDs) and of `otelcol_process_uptime`. Reuses `kubectl_json`, `prom_query`, `prom_status`. Done when: one run prints PASS/FAIL per item and fails on a missing component | K0c | SR15 Checker, K5 promoted list, K-D3, K-D12 | ◐ smoke + examples ran 2026-11-15; snapshot mode not built *Superseded 2026-11-23 (r6.2 table below).* |
| K-copy Collector copy acceptance (copy delivered 2026-11-19 by the platform session): `telemetrygen` Job (soak-run, by digest) sends exactly N spans, log records and data points through `soak-otlp` → each `otel_libs_soak_*_total` grows by exactly N and the copy's receiver accepted / exporter sent agree; a demo api restarted through the hook shows in Tempo, Loki and Prometheus with its own `k8s.pod.name` (not `soak-agent`'s) — K2 through the copy; sentinels readable (sibling-success rule) | K1, DE13 | K-D10, SR15 Enrichment (K2), Sentinels | ☐ (delivery smoke: 10 spans → 10 counted, 10 accepted, 10 sent, platform session) *Superseded 2026-11-23 (r6.2 table below).* |
| K7 Health-span absence app-side (promoted 2026-11-20, AG9 not accepted): one canary container per language exporting traces `otlp,console` under `/health` / `grpc.health.v1` probes, health spans counted in its stdout (K-D6), a low fixed rate for a short window before K8a. Done when: 0 health spans per language with a positive control (a non-health request seen in the same stdout) | K-copy | SR8 health, SR15 Health (K7), AG9 | ☐ Deferred 2026-11-23 (r6.2, lean soak) — AG9 accepted; promotion trigger in the r6.2 deferred table |
| K8a Passive window ≥ 24 h (30 h, K0e-1), all languages (or rotation per pre-flight), apps on `soak-otlp`, canary header set: new `verify.py soak` mode applies the S-D5 rules per container (RSS and working set, threads, restarts, Python FDs, Go goroutines, .NET thread pool) over the window and prints PASS/FAIL per container × rule; `verify.py demo` passes at the start and at the end; guards (K-D13) and the laptop loop (K-D15) run throughout. Done when: a valid window and a committed summary | K-copy | SR15 Windows, Soak rules (K8a), Platform guards, Orchestration, SR4 leaks | ☐ *Superseded 2026-11-23 (r6.2 table below).* |
| K-cal Calibration (≤ 1 h): per language, spans and log records per request for each (route × scenario × inbound class) at the copy, log records also per `severity_number`; each language's worker first-drive timing (at start or at start + interval) for the K8b placement; the Tempo trace-by-ID read latency (p99, Kafka ingest; for the SR9 trace sample only, not the counts); quiesce waits; resume bounds per language; the K10 burst size per language (above 2048 items per process); k6 rate steps (values chosen at K-cal) — the loaded rate = the highest step with every guard and sentinel clean and `dropped_iterations` = 0, minus one step; floors (K-D13) adjusted from the reads (dated note). Done when: the values are recorded in the summary and `expected` tables exist per service and per severity *(r6.1, 2026-11-20: first-drive timing, per-severity expectations and burst size added)* | K8a | K-D11, K-D12, K-D13, SR15 Counting | ☐ *Superseded 2026-11-23 (r6.2 table below).* |
| K8b + K6 Loaded window 2 h at the K-cal rate (k6 Job, worker at 10800 s, window placed between two drives): Counting (copy and end-to-end, LOSS and DUPLICATES verdicts, no Tempo subtraction), per-`severity_number` Loki record counts per app against expected (AG6), Sentinels, mid-window platform-roll check (any roll invalidates), api → backend service-graph share, secret canary scan (`scan_secret`, `loki_hygiene`), SR9 orphan check on a sample of fetched traces, SR10 cross-language comparison of metric names / units / attribute keys and span names / kinds against the README gaps. Done when: a valid window and every verdict in the summary *(r6.1, 2026-11-20: was worker 3600 s with a Tempo subtraction; AG6 counts and the roll check added)* | K-cal | SR15 Windows, Load, Counting, Sentinels, Secret canary; SR0 (severities), SR3, SR4, SR9 (orphans), SR10 | ☐ *Superseded 2026-11-23 (r6.2 table below).* |
| K-shut + K3 + K5 at the end of the loaded window: api pod of each language deleted with k6 running (k6 `phase=pre/post` scenarios), exit ≤ grace, copy count ≥ pre-SIGTERM answered × spans; K3 identity on the new pods (new `service.instance.id`, old instance's last sample ≤ `finishedAt` + one export interval); K5 labels against the K1 promoted list and series count before / after (a rebuild or restart makes new series, stated) | K8b + K6 | SR15 Shutdown, Identity (K3), Labels (K5); SR7, SR12 | ☐ *Superseded 2026-11-23 (r6.2 table below).* |
| K8c Multi-worker Python (AG8 not accepted, 2026-11-20): one extra Python api pod (soak-run Deployment, demo image from DE14) running gunicorn with N ≥ 2 Uvicorn workers (`--preload`, helper `post_fork`), behind its own Service, k6 at the K-cal per-language rate for a short window. Done when: N distinct `service.instance.id` values for that pod in the copy's and Loki's records, Σ per-worker counts = expected, Prometheus series per worker recorded | K-shut + K3 + K5, DE14 | SR13 multi-worker, SR15 Multi-worker Python (K8c), D4, AG8 | ☐ Deferred 2026-11-23 (r6.2, lean soak) — AG8 accepted; promotion trigger in the r6.2 deferred table |
| K10 + K10b Failure modes (refused, hung, burst with a hung peer, back-pressure, redirect, black hole) and SR6 cases (wrong DNS name at start-up — the only DNS case, gRPC to 4318, no endpoint → `/metrics`), each in its own short window with the criteria of SR15 "Failure modes" / "Misconfiguration"; back-pressure flip requested from the platform session (the expected roll; any other roll invalidates); `not injected` reported with the observation (NetworkPolicy under ambient accepted as such) *(r6.1, 2026-11-20: runtime DNS mode removed, burst mode added — AG7)* | K-shut | SR15 Failure modes, Misconfiguration; SR5, SR6, SR13 (burst, back-pressure, fallback) | ☐ *Superseded 2026-11-23 (r6.2 table below).* |
| K4 Ambient vs `none` toggle (promoted 2026-11-20, AG10 not accepted): own window after K10, demo apps scaled to 0 while `istio.io/dataplane-mode` is switched on `dev-otel-libs` only, pods restarted in each mode, trap restores `ambient`; criteria of SR15 "Mesh (K4)" at a short k6 load. Done when: both modes measured and compared per route and class, or the pre-flight stops it (waypoint / mesh tracing found) | K10 + K10b | D11.5, SR15 Mesh (K4), AG10 | ☐ Deferred 2026-11-23 (r6.2, lean soak) — AG10 accepted; promotion trigger in the r6.2 deferred table |
| K11 Report and refutation: summary `reports/<run>.md` committed; refutation by `observability` + `dev` (scored per language and check); `code-review` of `verify.py soak` and the run loop; D10 and CHANGELOG link the summary; `testing/k8s/README.md` updated | K10 + K10b | Acceptance, change loop | ☐ *Superseded 2026-11-23 (r6.2 table below).* |
| K9 OBI vs SDK | — | principles 1, 7 | Deferred 2026-11-19, optional (not a gate); promotion: the owner creates the `staffops*` namespace |

## Owner rulings 2026-11-20 on r6 (K0d)

1. Accepted gaps signed: **AG1** (TLS), **AG2** (gateway tail sampling and
   load balancing), **AG3** (backend outage), **AG4** (VictoriaMetrics),
   **AG5** (only the `dev` environment). **Not accepted**, each covered:
   AG6 → K8b per-`severity_number` Loki counts; AG7 → a K10 burst mode on
   the hung sink; AG8 → K8c (with DE14); AG9 → K7 promoted; AG10 → K4
   promoted (demo scaled to 0 for its window).
2. Loaded-window worker `DEMO_INTERVAL_SECONDS=10800`, so the exact count
   does not depend on Tempo; the Tempo subtraction is removed.
3. DNS failure only at start-up (SR6); the runtime ExternalName switch is
   removed from K10.
4. Guard floors accepted as proposed (MemAvailable ≥ 2 GiB, idle CPU ≥ 15 %,
   root filesystem ≥ 10 % free), dated `unmeasured`, adjusted after K-cal. A
   NetworkPolicy not enforced under ambient is recorded `not injected`.
5. Any mid-window platform roll is flagged in the report and invalidates the
   window, unless it is the requested back-pressure flip.
6. `tools/trace-allow.txt` reasons updated so they no longer name the dropped
   Phase S tasks.


## Owner ruling 2026-11-23 — lean soak (K0d2)

Follows the two K0c reviews that BLOCKED r6.1 and estimated 19–28
engineering days for it.

1. **Kept**: the mesh and platform guard (pre-flight and in-run, K-D18); the
   24 h (30 h, K0e-1) passive window (leaks, threads, Python FDs, restarts, Go goroutines,
   the .NET thread pool; Node.js container-only); one 2 h loaded window with
   k6, counted in **bounds** on the fixed span families only (SERVER,
   CONSUMER, PRODUCER, the demo-scope spans and the RPC client / server
   spans; everything else and every log count report-only, K-D19); the
   refused, hung, back-pressure and A31 redirect modes; K-shut, K3 and K5.
2. **Accepted gaps** (superseding the 2026-11-20 "not accepted"): AG6
   per-severity logs, AG7 burst, AG8 multi-worker Python (DE14 and gunicorn
   dropped, no new dependency), AG9 health-span absence (K7), AG10 the K4
   mesh toggle, and new AG11 the NetworkPolicy black hole.
3. K4, K7, K8c and the burst row stay, deferred, with promotion triggers.

## Phase K r6.2 — lean path (added 2026-11-23)

Ordered by dependency. Effort is engineering days (design, code, tests,
review fixes), excluding wall-clock waits, which are listed separately;
every figure is an estimate, not a measurement. Target: about 6–9 days.

| Task | Depends on | Covers | Effort (days) | Status |
|---|---|---|---|---|
| K0d2 Owner ruling: lean soak (section above) | K0c | Acceptance, AG6–AG11 | — | ✅ 2026-11-23 (owner) |
| K0e Adversarial review of r6.2: two independent reviewers (`observability` + `spec-refuter` or `dev`), limited to the lean scope (owner rulings K0e-1…K0e-5 of 2026-11-23 recorded below; AG12 signed) | K0d2 | format rule | 0.25–0.5 | ◐ 2026-11-23 — review NOT READY (2 BLOCKING, 6 HIGH, 7 MEDIUM, 4 LOW); owner rulings K0e-1…K0e-5 and every finding applied as dated r6.2 corrections (section below); re-check is K0f |
| K0f Re-check of the K0e corrections by the K0e reviewer (B1, B2, H1–H6 at least) | K0e | format rule | 0.25 | ☐ |
| DE13 (demo-environment) env-override hook in `demo.sh`, amended 2026-11-23: `DEMO_ENV_FILE` as DE-D7; `DEMO_RESTART_NONCE` (an allowed `DEMO_*`) forces a roll; `demo.sh scale --lang or --all <n>`; the allow-list denies `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`. Done when: the demo spec's DE13 tests pass, plus nonce, scale and both denials | K0e | SR15 Restart mechanics (r6.2), Windows, K-D11 | 0.5–0.75 | ☐ |
| K-hang `testing/fakeotlp --hang`: completes the TCP and HTTP/2 (h2c) handshake, accepts the gRPC stream or HTTP request and blocks Export until the client gives up; unit test; image built from `testing/fakeotlp/Dockerfile` with the flags of `testing/k8s/build-images.sh`, pushed to `harbor.staffops.space/development/otel-libs-fakeotlp:<tag>`, digest recorded. Done when: a Go client export against `--hang` times out at its configured timeout in the test, and the image digest is in the K1 snapshot | K0e | SR15 Failure modes (r6.2) *hung*, *redirect* | 0.25–0.5 | ☐ |
| K1 Platform snapshot, lite (r6 row) plus: the measured K8a runtime-metric names (SR15 Soak rules r6.2 note), the `exported_instance` label on the copy series (`unmeasured` 2026-11-23: no series in 2 days), a 1 h reading of MemAvailable, idle CPU and root filesystem free that sets the K-D13 floors, the Prometheus PVC capacity vs request, the roll list's pod names and start times, that PID 1 is the app in every demo container (for the FD `exec` read), the Tempo attribute set of the demo services (selectors taken from it; a `service.namespace` filter returned nothing 2026-11-23). Done when: one run prints PASS/FAIL per item and fails on a missing component *K0e corrections 2026-11-23:* a 24 h baseline of every sentinel and guard counter (B1) and a dated per-edge baseline of `istio_tcp_connections_failed_total` for demo → `soak-agent` and `soak-agent` → `otel-agent-collector` (B2); every shared-job query with `cluster="k8s01"` (H5). | K0e | SR15 Checker, Floors and capacity (r6.2), K-D3, K-D12, K-D13 | 0.5 | ◐ smoke + examples ran 2026-11-15; snapshot mode not built |
| K-loop Run loop in `testing/k8s/soak/`: the laptop loop under `caffeinate`, the guards (K-D13) and the mesh and platform guard (K-D18), the heartbeat; an EXIT / INT / TERM trap that saves `kubectl logs job/<k6>` first, deletes only `otel-libs/soak-run=<run>` objects, restores the namespace label if it is not `ambient`, re-applies the demo; `cleanup --run <id>`; a pre-flight that refuses to start while any soak-run object exists; renderers for every soak-run object (k6 Job and ConfigMap, `soak-otlp*` Services, fakeotlp pods and Services, telemetrygen Job, the SR6 fallback Service); the k6 script generator (per-cell no-op thresholds, outcome tags, `maxVUs`, `activeDeadlineSeconds`, the 50-per-cell reservoir, `phase=pre or post`); the `.gitignore` line for `testing/k8s/soak/runs/`; unit tests (≥ 90 % lines). Done when: a dry run creates one object of each kind and the trap deletes exactly those (and nothing else) *K0e corrections 2026-11-23:* the entry gate (sentinel and guard counters clean over 30 min) before K8a, K-cal, K8b and each K10 mode, pointing to the platform session on failure (B1); guard (b) on the two telemetry edges only, other edges report-only findings (B2); (d) exempts the two back-pressure flips, (h) at window start only (M1, M2); tests prove the k6 reservoir on the pinned k6, with the bounded-stdout fallback (M5), and the guarded label no-op (L3); the FD exec read at every 60 s guard read, 12 calls a minute (L2). | K1, DE13 | SR15 Orchestration (r6.2), Mesh and platform guard, Load (r6.2), Evidence (r6.2), K-D15 | 1.25–1.5 | ☐ |
| K-count `verify.py soak` readers (family verdicts from the OTTL-filtered copy metric when it exists, else the gateway `span_metrics`, source stated per verdict — ruling K0e-3; the FD `exec` sweep reader — ruling K0e-2): raw range reads with the "Counting in bounds" series rules (first sample, reappearance, sums over `pod` / `instance` / `exported_instance`, `service.version`), the interval verdicts LOSS / DUPLICATES, "Sentinels (r6.2)" with the sibling rule, the gateway-restart detector (`restartCount`, `startedAt`), the S-D5 soak rules per container; unit tests on recorded fixtures (≥ 90 % lines). Done when: the tests cover a reset, an expiry and reappearance, a series first seen inside the window and a no-answer bound *K0e corrections 2026-11-23:* copy verdict = `otel_libs_soak_fixed_spans_total` per `service_instance_id` (no family label), gateway per-family counts summed by (`service_name`, `span_kind`, `span_name`) as a cross-check under clean shared sentinels (H2, M3); sentinels scoped by verdict (B1); fixture tests for a gap ≥ expiry (reset, adds v) and a shorter gap (v − last, or v when v < last), with copy 30 m and gateway 5 m (H4); a test that a `cluster="vps"` series never enters a read (H5). *Updated 2026-11-23:* any demo-service series with `service_instance_id="unset"` is reported as a finding (D4) and invalidates that service's per-instance verdict; fixture test. | K1 | SR15 Counting in bounds, Sentinels (r6.2), Soak rules (K8a), K-D19 | 1–1.25 | ☐ |
| K-copy Collector copy acceptance (r6 row): the telemetrygen positive control and K2 through the copy, plus guard item (h); once the platform session delivers it (ruling K0e-3, requested 2026-11-23), the OTTL-filtered fixed-family `count` metric per sending instance is accepted with the same positive control. Done when: N sent = N counted = N accepted and the demo pods' `k8s.pod.name` is seen (and, if delivered, the filtered metric counts exactly the fixed-family spans of the control) *K0e corrections 2026-11-23:* the positive control sends one CLIENT gRPC span with `rpc.system` and one with `rpc.system.name` and expects both in `otel_libs_soak_fixed_spans_total` once the platform session adds the `rpc.system.name` clause (H1); live facts recorded — expiry 30 m, labels incl. `service_instance_id` and `exported_instance`, smoke 10 spans / 5 fixed (H2). *Updated 2026-11-23 (soak-agent 1f56009 + b4c1dae):* H1 closed — the `rpc.system.name` clause is live (delivery smoke: `rpc.system` 4/4 fixed, `rpc.system.name` 4/4, neither 2/4); the control also sends one span without `service.instance.id` and expects it under `service_instance_id="unset"`, never dropped. | K-loop, K-count | K-D10, SR15 Enrichment (K2), Sentinels (r6.2) | 0.25 | ☐ |
| K8a Passive window 30 h = 12 h warm-up + 18 h judged (owner ruling K0e-1, 2026-11-23; was 6 h + 18 h; S-D5 recalibration: slope rule over the judged span and its second half, ratio report-only), all languages (rotation only if the pre-flight requires), apps on `soak-otlp`, canary header set; 30 min clean guard lookback and the coverage gate first (span metrics flowing, runtime coverage ≥ 95 % per language that has them); FDs in all four languages by the named `exec` read `ls /proc/1/fd *K0e corrections 2026-11-23:* starts only after the entry gate and after platform changes have settled (no roll-list roll, no platform-session change in 30 min, L4); running image tags equal `testing/k8s/demo/demo-tag.sh` at `soak/<run>` (M4). | wc -l` per demo container per sweep (ruling K0e-2), Node.js runtime state not judged (AG12, accepted); watch list discussed; `verify.py demo` at start and end. Done when: a valid window and its section of the summary | K-copy | SR15 Windows, Soak rules (K8a), Mesh and platform guard, SR4 leaks | 0.25–0.5 | ☐ |
| K-cal Calibration: per (route × scenario × class), two repeats of N requests; fixed-family k values and the copy-total check (integer and equal, else excluded and named); each worker's observed first `demo.drive`; drain rate per language; quiesce waits; resume bounds from upstream backoff; k6 rate steps → loaded rate (highest clean step, minus one); shared-queue floor. Duration `unmeasured` (estimate 3–6 h wall clock). Done when: the values and the exclusions are in the summary | K8a | SR15 Counting in bounds, Windows and drain, K-D19 | 0.5–0.75 | ☐ |
| K8b Loaded window 2 h at the K-cal rate (worker `DEMO_INTERVAL_SECONDS=10800`, window between drives, t1 ≤ 4 min after the last traffic, drain rule): fixed-family bounds, report-only counts (other spans, logs, per severity), Sentinels (r6.2), drain check, api → backend service-graph share, secret canary, SR9 orphan check on the reservoir, SR10 comparison. Done when: a valid window and every verdict in the summary *K0e corrections 2026-11-23:* the t1 ≤ 4 min rule is dropped (H4); t1 for Go follows the worker's `startedAt` + quiesce, for the others the observed first drive (H6). | K-cal | SR15 Windows and drain, Load (r6.2), Counting in bounds, Sentinels (r6.2), Secret canary; SR3, SR4, SR9, SR10 | 0.25–0.5 | ☐ |
| K-shut + K3 + K5 after t2, in its own k6 run (`phase=pre or post`, own t1'): each api pod deleted by name at the boundary + δ (δ from the k6 container's `startedAt`), exit within grace, the old process's count by `exported_instance` in its bound (report-only for an api whose copy total K-cal did not qualify); K3 on a `DEMO_RESTART_NONCE` roll; K5 labels against the K1 promoted list, series count before and after. Done when: every verdict in the summary *K0e correction 2026-11-23:* the old process's count from `otel_libs_soak_fixed_spans_total{service_instance_id="<old>"}` × the fixed-family k, no-answer requests in the upper bound (H3). | K8b | SR15 Shutdown (r6.2), Restart mechanics (r6.2), Identity (K3), Labels (K5); SR7, SR12 | 0.5–0.75 | ☐ |
| K10 + K10b Failure modes refused, hung (`fakeotlp --hang`), back-pressure (platform session flip; window from new pod Ready + first refusal to the next Ready, pod names recorded), redirect (`fakeotlp` 307, one language at a time on `http/protobuf`); SR6 cases (DNS name at start-up, gRPC to 4318, no endpoint → `/metrics`). Criteria of SR15 "Failure modes" with the r6.2 changes (failure-line interval proxy, resume bound). Done when: each mode passes or is `not injected` with the observation | K-shut + K3 + K5, K-hang | SR15 Failure modes (r6.2), Misconfiguration; SR5, SR6, SR13 (back-pressure, fallback) | 0.5–0.75 | ☐ |
| K11 Report and refutation: `scan_secret` over `reports/<run>.md` (canary, `parity-redirect-token`); code-freeze check (`git diff soak/<run>..HEAD -- go python nodejs/packages dotnet` empty; the local tag is created at run start, the owner pushes it); summary committed; refutation by `observability` + `dev` (scored per language and check); `code-review` of `testing/k8s/soak/` and `verify.py soak`; D10 and CHANGELOG link the summary; `testing/k8s/README.md` updated *K0e correction 2026-11-23:* the freeze diff runs over `testing/k8s/demo/demo-tag.sh`'s input list (adds `nodejs/demo`, `nodejs/package*.json` and the other image inputs), image tags checked against `demo-tag.sh` at `soak/<run>`; a runtime-behavior change also re-runs the passive window (M4, K-D16). | K0e, DE13, K-hang, K1, K-loop, K-count, K-copy, K8a, K-cal, K8b, K-shut + K3 + K5, K10 + K10b | Acceptance (r6.2), change loop | 0.5 | ☐ |

**Effort total**: about 6.75–9.5 engineering days (sum of the column; *K0e corrections 2026-11-23: +0.25 K0f, +0.25 K-count*).
**Wall clock, not in the total** (estimates, `unmeasured`): 24 h sentinel
and guard baseline (K1, K0e B1; the 1 h floor reading runs inside it); 30 min lookback + 30 h passive window (K8a; owner ruling K0e-1); 3–6 h K-cal;
2 h loaded window plus quiesce (K8b); about 30 min K-shut; about 30 min per
K10 mode and SR6 case. The soaked commit gets the local tag `soak/<run>` before K8a (owner pushes tags; code freeze,
SR15 "Evidence (r6.2)").

### Deferred in r6.2 (2026-11-23)

| Task | Gap | Status | Promotion trigger |
|---|---|---|---|
| K4 Ambient vs `none` toggle (r6.1 row) | AG10 | Deferred 2026-11-23 | the mesh guard fires on a window with no other explanation; a waypoint or mesh tracing is planned for the cluster; or a user reports a mesh-related trace difference |
| K7 Health-span absence app-side (r6.1 row) | AG9 | Deferred 2026-11-23 | the platform drops or changes `filter/noise`; a health-span report from a user; or a change to the helper health filter (D5) |
| K8c Multi-worker Python (r6.1 row; needs DE14, dropped) | AG8 | Deferred 2026-11-23 | a supported multi-worker Python deployment is documented, or a D4 fork-path change lands |
| K10-burst Burst with a hung peer (r6.1 mode) | AG7 | Deferred 2026-11-23 | a silent drop is reported in another context, or a batch-queue change lands in any language |
| K8b-sev Per-`severity_number` log counts as verdicts (r6.1) | AG6 | Deferred 2026-11-23 (counts recorded report-only in K8b) | a log-loss report, or the report-only counts in K8b show a loss pattern |
| K10-blackhole NetworkPolicy black hole (r6.1 mode) | AG11 | Dropped 2026-11-23 | ambient enforcement of namespaced egress NetworkPolicies is measured on the cluster |
| K9 OBI vs SDK | — | Deferred 2026-11-19 | the owner creates the `staffops*` namespace |

## Owner rulings 2026-11-23 on the r6.2 open questions (K0e decisions)

Recorded before the K0e review itself (which stays open).

1. **K0e-1** K8a is 30 h: a 12 h warm-up and 18 h judged (replaces 6 h +
   18 h). S-D5 note, the K8a row and the wall-clock estimate updated.
2. **K0e-2** FDs are measured in all four languages with
   `kubectl exec … sh -c 'ls /proc/1/fd | wc -l'` in each demo container at
   every sweep — a named, read-only soak-run read and the one `exec`
   exception in SR15 "Checker". AG12 is kept only for "no Node.js runtime
   metrics", accepted by the owner as part of the lean scope.
3. **K0e-3** The OTTL-filtered `count` metric (fixed span families only, per
   sending instance) is requested from the platform session now (the
   coordinator sends the request). Family verdicts use it once it exists and
   fall back to the gateway `span_metrics` until then (K-copy, K-count,
   K-D19).
4. **K0e-4** The soaked commit gets a local git tag `soak/<run>`; the owner
   pushes tags; the code freeze is checked against that tag.
5. **K0e-5** The trap's namespace-label restore stays a guarded no-op.

## K0e review corrections (2026-11-23)

The K0e review returned NOT READY (2 BLOCKING, 6 HIGH). Applied as dated r6.2
corrections in requirements SR15 (inline notes and the "K0e corrections"
bullet) and design K-D12, K-D15, K-D18, K-D19:

- **B1** K1 24 h baseline of every sentinel and guard counter; entry gate
  (clean 30 min lookback) before K8a, K-cal, K8b and K10, pointing to the
  platform session; sentinels scoped by verdict — copy sentinels gate copy
  counts, shared sentinels gate only end-to-end checks.
- **B2** guard (b) on the two telemetry edges against a K1 per-edge baseline;
  other edges report-only findings.
- **H1** `rpc.system.name` clause requested; K-copy control sends both keys.
  *Closed 2026-11-23 (soak-agent 1f56009 + b4c1dae; smoke 4/4, 4/4, 2/4).*
- **H2** live copy facts (30 m expiry, `otel_libs_soak_fixed_spans_total`
  labels, smoke 10 / 5, `exported_instance` measured); one fixed total per
  `service_instance_id`, gateway families as cross-check.
- **H3** K-shut on `otel_libs_soak_fixed_spans_total{service_instance_id}`.
- **H4** reappearance by gap vs expiry; "t1 ≤ 4 min" dropped; fixture tests.
- **H5** `cluster="k8s01"` on every shared-job query; test.
- **H6** Go t1 from the worker's `startedAt` + quiesce.
- **M1–M6, L1–L4** as listed in the K0e review; M7 covered by H2.

Context measured by the coordinator (Prometheus MCP, 2026-11-23):
`increase(…[15m])` was 0 for the refused / send-failed counters and
`istio_tcp_connections_failed_total`; the reviewer's 1–3 h windows had
mostly covered the broken-mesh period before the platform fix.
