# Soak simulation — tasks

**Status**: Draft r4 (2026-10-28). Spec: [requirements](requirements.md) ·
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

| Task | Covers | Status |
|---|---|---|
| K1 `kind` + Collector agent DaemonSet / gateway Deployment via the platform charts (or faithful copies) | D11.3, D11.4 | Deferred |
| K2 `k8sattributes` enrichment visible in all backends; apps set only `service.name`/`deployment.environment.name` | AGENTS.md principle 4 | Deferred |
| K3 Pod identity: semconv K8s recipe for single-process pods; no series duplication on rollout | D4, D11.4 | Deferred |
| K4 Istio ambient/sidecar tracing at 100% vs disabled under ParentBased | D11.5, ADR-002 | Deferred |
| K5 VictoriaMetrics label set with real pod attributes, rollout restarts | D11.6 | Deferred |

**Promotion trigger**: any of — a cluster (kind or real) is available and
Phase S passed; a Phase S result that cannot be explained without K8s; the
platform team (A12) asks for chart-level evidence; before `0.3.0` reaches a
Kubernetes PRD rollout.
