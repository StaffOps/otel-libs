# Soak simulation — tasks

**Status**: Draft r4 (2026-10-28); Phase K r5 draft (2026-11-15, not yet
reviewed — K0). Spec: [requirements](requirements.md) ·
[design](design.md). Feeds: config-contract-v0.3 D10 (rc soak) → `0.3.0` final.

## Phase S — Docker Compose simulation (now)

| Task | Depends on | Covers | Status |
|---|---|---|---|
| S0 Adversarial review r1 (`observability` + `dev`) | — | format rule | ✅ 2026-10-28 (r2 written) |
| S0b Re-review r2 (`observability` + `dev`) | S0 | format rule | ✅ 2026-10-28 (r3 written) |
| S0c Re-review r3 (dev: clear; observability: 2 HIGH) | S0b | format rule | ✅ 2026-10-28 (r4 written) |
| S0d Re-review r4 (observability) | S0c | format rule | ✅ 2026-10-28 (no BLOCKING/HIGH; last MEDIUM/LOWs applied) |
| S1 Stack: agent (+ `agent-net` holder), 2 gateways, Tempo, Loki, VictoriaMetrics, Grafana, private CA (reuse fakeotlp cert mode), pinned versions, internal telemetry readers, dedicated buildx builder | S0d | SR1, S-D2, S-D7, S-D8, S-D12 | ☐ |
| S2 Soak apps Go + Python (SR0 contract) + Dockerfiles in released / `--from-tree` modes; **build check of the 0.2 apps first** (Go 0.2.1 deps on `golang:1.26`; if it fails, a `golang:1.22` builder enters the disk budget) | S0d | SR0, S-D9, S-D10 | ☐ |
| S3 k6 open-loop load script (route mix, inbound traceparent 00/01, client-side log) | S2 | SR3 | ☐ |
| S4a Checker: count pipelines + **positive control** (telemetrygen), completeness + loss accounting | S1–S3 | SR4, S-D6 | ☐ |
| S4b Failure modes refused / black hole / hung / DNS | S4a | SR5, S-D12 | ☐ |
| S4c Misconfiguration cases (separate phase) | S4a | SR6 | ☐ |
| S4d Shutdown | S4a | SR7 | ☐ |
| S4e Environment behavior: logs, sampling formulas, prd-debug bounds, dev-debug phase, traceparent 00 phase, health at agent | S4a | SR8, S-D8 | ☐ |
| S4f Trace integrity + gateway-1 restart | S4a | SR9 | ☐ |
| S4g Parity sets | S4a | SR10 | ☐ |
| S4h Restart identity | S4a | SR12 | ☐ |
| S4i Burst, backend outage, back-pressure, multi-worker, Prometheus fallback | S4a | SR13 | ☐ |
| S5 0.2 apps (own source, separate go.mod, constraints, always-sample path; build them first) + migration query set (separate phase) | S4a | SR11, S-D9 | ☐ |
| S6 `run.sh` (phases, timeline, stats sampling, pre-flight, disk monitor, sleep guard, trap, purge) + report writer | S4a | SR14, S-D3, S-D4, S-D7 | ☐ |
| S7 Calibration + short run on `0.3.0-rc.2`: record mem_limit, ingest rate, p99 trace duration + batch delays → `decision_wait`, spans per route, max spans per trace (→ R), resume bounds, durations; fix the stack until trustworthy | S6 | S-D3, S-D5, S-D8 | ☐ |
| S8 Full run on the last rc; fix lib defects (new rc), re-run until it passes | S7 | Acceptance | ☐ |
| S9 Report refutation (`observability` + `dev`), `code-review` of the stack | S8 | change loop | ☐ |
| S10 Record result in D10 + CHANGELOG; `testing/soak/README.md`; README gaps list (runtime metric families) | S9 | change loop, SR10 | ☐ |

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
| K0 Adversarial review of r5 (`observability` + `dev`) | — | format rule | ☐ |
| K0b Owner rulings: trigger amendment; slot budget and platform reserve; ambient toggle on `dev-otel-libs`; OBI namespace; reference platform for Phase S (homelab Prometheus or the A12 VictoriaMetrics) | K0 | K-D1, K-D2, K-D5, K-D7, K-D9 | ☐ *Owner rulings 2026-11-15:* (1) trigger amended — a cluster being available is enough to run Phase K now, as additional evidence; soak acceptance (D10) still needs Phase S, which covers the gateway tail sampling and load balancing this cluster lacks; (2) the K4 ambient toggle is allowed: `istio.io/dataplane-mode=none` on `dev-otel-libs` only for the K4 window, restored by the run trap (Kyverno `namespace-ambient-mode` only acts when the label is missing); (3) slot budget: the owner raised the kubelet `maxPods` (value recorded in the K1 report) instead of a PriorityClass or a script reserve; the script pre-flight check stays as a guard. Open: OBI namespace (K9), the Phase S reference platform (K5), `k8s.container.name` in multi-container pods, K10 failure modes under ambient. |
| K1 Use the existing platform pipeline (replaces `kind`): `verify.py platform` records the three Collector CRs (credentials stripped), Prometheus `otlp` settings, backend versions, the namespace ambient label, every Gateway API `Gateway` and the node's allocatable / requested CPU, memory and pods in the report, and fails when a component a K check relies on is missing or changed. New code in `verify.py`: a Prometheus `query_range` client and a read-only `kubectl get -o json` wrapper (it has neither today, K-D3) | K0b | D11.3, D11.4, SR15, K-D1, K-D3 | ◐ smoke + examples ran 2026-11-15 (`testing/k8s/README.md` "Measured results"); snapshot mode not built |
| K2 `k8s_attributes` enrichment asserted in Tempo, Loki and Prometheus; the apps set only `service.name` / `deployment.environment.name` (+ the `otel_libs.run` test tag); `k8s.container.name` asserted in single-container pods only (K-D4) | K1 | principle 4, SR15 | ◐ Tempo 8/8 smoke resources (`verify.py smoke --run r1`); Loki and Prometheus not asserted per service; multi-container pods lack `k8s.container.name` |
| K3 Pod identity: the helper's `service.instance.id` reaches every backend unchanged (the agent does not extract it); new id per process after `rollout restart`; old `instance` has no sample later than the old container's `terminated.finishedAt` + one export interval, sample time read with `max_over_time(timestamp(…)[w:<step>])` or a raw range selector, not `timestamp(last_over_time(…))` (SR12, SR15); semconv K8s recipe variant via the Downward API in one single-process Deployment | K1 | D4, D11.4, SR12, SR15 | ☐ |
| K4 Istio ambient (ztunnel L4, no waypoint) vs `istio.io/dataplane-mode=none` on `dev-otel-libs` only, `split` layout so the api → backend hop crosses ztunnel: same spans per route, same parent links, enrichment intact, no mesh-generated span; label restored by the run trap | K0b, K1 | D11.5, ADR-002, K-D5 | ☐ |
| K5 Prometheus label set (replaces the VictoriaMetrics row): `job` = `<service.namespace>/<service.name>`, `instance` = `service.instance.id`, the eleven promoted attributes; `deployment.environment.name` only on `target_info` (join); series count per rollout restart | K1 | D11.6 (Prometheus variant), SR10, SR12, K-D9 | ☐ |
| K6 Gateway connectors as soak signals: raw `traces_span_metrics_calls_total` per `job` × `span_name` at two quiesced checkpoints (t2 − t1; a series absent at t1 counts its t2 value; a reset invalidates the window) vs load-generator answered requests × calibrated spans per route, exact; `traces_service_graph_request_{total,failed_total}` per edge vs the load generator's 5xx share; `otelcol_connector_servicegraph_expired_edges` reported | K1, S3 | SR4 (cluster variant), SR15 | ☐ |
| K7 Health-span absence proven app-side: the agent's `filter/noise` drops `/health` (and `/metrics`, `/ready`, …) before every backend, so absence in Tempo proves nothing about the helper (K-D6) | K1 | D5, SR8 (health) | ☐ |
| K8 Cluster steady soak on the S2 soak apps: one language per window, `split` layout (load generator in its own pod), every container with explicit requests that pass the K-D2 pre-flight, languages rotated unless the budget fits more (K-D2); Loki records counted by `service_instance_id` across `__stream_shard__` streams; S-D5 leak rules on cAdvisor `container_memory_rss` / `container_threads`; restarts from `kube_pod_container_status_restarts_total`; FDs `n/a` (no source, K-D3) | K0b, K2, S2, S3 | SR4, S-D5, SR15 | ☐ |
| K9 OBI vs SDK, opt-in experiment in an owner-created `staffops*` namespace: OBI's rule `exclude-otlp-exporters` (OTLP/protobuf to port 4317) keeps OBI off helper processes — no duplicate spans; which helper transports it matches is `unmeasured`; optional uninstrumented twin for a span-shape comparison (K-D7) | K0b, K1 | principles 1, 7 (evidence only) | ☐ opt-in, not an acceptance gate |
| K10 Failure modes without touching shared Collectors (K-D8): DNS failure (endpoint Service name that does not exist), refused/timeout (Service with no endpoints — behavior under ambient `unmeasured`), black hole (namespaced egress NetworkPolicy — effect under ambient `unmeasured`) | K0b, K8 | SR5 subset | ☐ |
| K11 Report, refutation (`observability` + `dev`), `testing/k8s/README.md` + CHANGELOG | K2–K8 | change loop | ☐ |

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
