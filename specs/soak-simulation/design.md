# Soak simulation — design

**Status**: Draft r4 (2026-10-28). Requirements: [requirements.md](requirements.md).
r1 superseded after the S0 review (see the requirements r2 note).
r5 (2026-11-15, draft, not yet reviewed — K0): Phase K on `k8s01`, decisions
K-D1…K-D9 at the end; dated notes on S-D1 and S-D2.

## Topology (reference)

```
load generator (k6, open loop) ─HTTP─▶ api ─gRPC─▶ backend        worker ─HTTP─▶ api (prd only, on command)
        apps per language × {dev, hml, prd, unset, prd-debug} (matrix S-D11)
            │ OTLP gRPC | HTTP | TLS (private CA)
            ▼
 agent-net (netns holder, iptables)  ⇐ agent Collector (network_mode: service:agent-net)
   receivers otlp → memory_limiter → count connector (per service × env × signal) → batch
   traces: loadbalancing (routing_key traceID, static resolver) → gateway-0 | gateway-1
   metrics/logs: → gateway-0          prd-debug: own receiver port + pipeline (S-D8)
            ▼
 gateway-0 / gateway-1: memory_limiter → filter(health) → tail_sampling → resource(gateway.id) → batch
            ▼
   Tempo · Loki (OTLP) · VictoriaMetrics (OTLP) · Grafana
 checker container (no Docker socket): backend HTTP APIs + Collector :8888 + host stats files
 host run.sh: docker compose -p otel-libs-soak … (inject faults, signals, stats sampling)
```

Pinned versions (recorded with digests): Collector contrib 0.160.0 (as e2e);
Tempo, Loki, VictoriaMetrics, Grafana, k6 pinned in the compose file at S1.

## Decisions

### S-D1 — Docker Compose now, Kubernetes later (Phase K)
**Choice**: Compose project `otel-libs-soak`.
**Justification**: SR0–SR14 are process/pipeline-level; reuses the Docker-only
workflow and the e2e Collector version.
**Trade-offs**: no `k8sattributes`, pod identity, mesh (Phase K). Whether it
"fits one laptop" is `unmeasured` — the pre-flight (S-D7) decides per run.
**When wrong**: a failure reproduces only with K8s networking/enrichment.
**Reversal action**: promote Phase K; the checker talks HTTP only, so it runs
unchanged against `kind`; replace `run.sh` orchestration with `kubectl`.
**Alternatives**: `kind` now (owner deferred); a cloud cluster (no
environment; data would leave the machine).
*(Corrected 2026-11-15, r5: the reversal action assumed the S-D4 checker would
"run unchanged against `kind`". It cannot be the route taken: S4a is not
built, and the cluster now available (`k8s01`) has no tail-sampling or
load-balancing gateway, so the sampling and routing checks have nothing to
read there. Phase K uses `testing/k8s/verify.py` and the platform pipeline
instead — K-D1, K-D3.)*

### S-D2 — Backends: Tempo, Loki, VictoriaMetrics single-node, Grafana
**Choice**: the platform's backend family, single-node, OTLP ingest.
VictoriaMetrics with Prometheus naming (SR1 flags); Loki with structured
metadata (default ≥ 3.0) and compactor retention.
**Justification**: SR8/SR10/SR11 are claims about stored data and naming.
**Trade-offs**: ~4 more images.
**When wrong**: the platform changes backends.
**Reversal action**: swap the service and its checker adapter.
**Alternatives**: Collector `debug`/`file` only (no naming/label evidence).
*(Amended 2026-11-15, r5: the homelab platform stores metrics in Prometheus
`3.16.0` through its OTLP receiver, not VictoriaMetrics — measured on
`k8s01`. Phase S keeps VictoriaMetrics until the owner rules which platform
is the reference (K0b); Phase K reads Prometheus — K-D9.)*

### S-D3 — Phases and durations
**Choice**: order = calibration (5 min, sampling 100%) → steady phase →
failure scenarios → teardown. Short run: steady ≥ warm-up + 30 min, leak
checks `n/a`. Full run: steady ≥ 2 h (parameter). Leak analysis excludes
failure windows. All durations `unmeasured` until S7 records them.
**Justification**: leak windows need ≥ 2 × 15 min after warm-up; failures
must not pollute leak data.
**When wrong**: leaks appear only after > 2 h.
**Reversal action**: raise the steady parameter.
**Alternatives**: 24 h (no evidence it is needed yet).

### S-D4 — Host orchestrates; checker has no socket
**Choice**: `run.sh` (host) does every Docker action through `docker compose
-p otel-libs-soak` (`kill -s TERM`, `stop/start`, `pause/unpause`, `exec`
for iptables in `agent-net`, `/proc` reads) and writes a timeline + stats
files; the checker container reads those files, backend APIs (TraceQL fetch
by ID, LogQL, MetricsQL/export) and Collector `:8888`. Export-attempt timing
comes from `docker compose logs --timestamps` lines: Go helper/SDK errors on
stderr (`otelhelper`/`otel` error lines), Python `otel_helper`/
`opentelemetry.exporter` logger lines — exact patterns fixed in S4.
*(Amended 2026-10-29, ADR-011: the Python helper loggers are
`staffops_otel_helper.*`; match that hierarchy, not `otel_helper`.)*
**Justification**: a socket in a container controls every container on a
shared daemon and can bind-mount host paths (`:ro` does not restrict the
API); Compose scopes by project label.
Allowed read-only host calls outside Compose: `docker info`, `docker
system df`, and `docker inspect`/`stats` filtered by label
`com.docker.compose.project=otel-libs-soak`.
**When wrong**: an action Compose cannot express.
**Reversal action**: a socket proxy allowlisting `stop|start|kill|pause|
unpause|stats|exec` on containers labeled `com.docker.compose.project=
otel-libs-soak`, denying `create`.

### S-D5 — Criteria (initial, owner-chosen, recalibrated after S7)

| Check | Source | Criterion |
|---|---|---|
| Memory | one `docker exec` per container per sweep, in parallel: `cat /sys/fs/cgroup/memory.stat /sys/fs/cgroup/memory.swap.current; grep -E 'VmRSS|Threads' /proc/1/status; ls /proc/1/fd \| wc -l` (`anon`, swap, threads, FDs); every 15 s, sweep time measured at S7 (fallback: Docker API one-shot stats for `anon`, exec for threads/FDs at a lower rate); Go also `go.memory.used` | slope over last 75% of steady ≤ 1% of median per hour **and** last-15-min median ≤ first-15-min-after-warm-up median × 1.10. Minimum detectable leak `unmeasured` until two runs give the noise floor |
| Threads / FDs | same sweep | same rule |
| Restarts | `compose ps` / exit codes | 0 |
| Completeness | agent `count` connector vs canary console counts; Collector/backend counters (S-D6) | equal at quiesced checkpoints; all downstream loss accounted |
| Sampling rates (dev/hml/prd/unset) | gateway post-sampling `count` (numerator) / agent entry-span `count` (denominator, error share); `increase()` across resets | observed share within ±3σ of the S-D8 formula (σ = √(p(1−p)/n)); n ≥ 1000 traces per env or `insufficient-data` |
| prd-debug | gateway pre/post-sampling `count` (λ_g, retained) | per gateway, run aggregate (60 s windows informative): max(min(R, λ_g), (0.1 + e)·λ_g) − tol ≤ retained/s ≤ R + (0.1 + e)·λ_g + tol, tol = 3σ of the probabilistic term (traces counted) + one trace's span count; + one 2R burst after each idle gap ≥ 2 s; retained share ≪ 1 when λ_g ≫ R |
| Resume after failure | first successful export after recovery | ≤ documented backoff bound (stated in report; `unmeasured` until S7) |
| Export timeout | diagnostic timestamps | attempt end − start ≤ timeout + 1 s |
| Request impact | load-generator log | 0 telemetry-caused errors; p99 ≤ pre-failure p99 + export timeout |
| Shutdown | timeline + agent counts | exit ≤ 10 s; produced = accepted |
| Trace integrity | Tempo fetch by ID, N = 200 per env | one `gateway.id` per trace; 0 orphans outside a restart window (the entry span's k6-logged parent is not an orphan) |
| Parity | VictoriaMetrics / Tempo sets | difference = documented gaps only |
| Run validity | host | invalid on host sleep (gap > 3 × sample interval), disk below floor, neighbour CPU > 50% of the VM for > 5 min, neighbour memory growth > 1 GiB over the run, or an OOM kill of a soak container whose `anon` was < 80% of its `mem_limit` (VM-wide OOM, not a leak) |

**When wrong**: thresholds misfire on the first run. **Reversal action**:
adjust with run data; record the change dated here.

### S-D6 — Counting and loss accounting
**Choice**: per signal, at quiesced checkpoints:
- produced (canary only): the canary set runs at a low fixed rate (recorded
  parameter) exporting `otlp,console`; `run.sh` follows **stdout only**
  (`docker logs -f --timestamps <id> 2>/dev/null`, id resolved by project
  label — stderr diagnostics would corrupt multi-line JSON) into a host-side
  counter that parses JSON objects (Python prints multi-line JSON), skips
  non-JSON lines and counts per object type (span, log record, datapoints
  per metrics export); it persists the last timestamp and resumes with
  `--since` after a restart (a gap invalidates the window). The canary keeps
  a json-file cap too (a live follower survives rotation) and its output
  (rate × bytes per item × duration) is in the S-D7 disk budget; Go SDK
  opt-in self-observability metrics if available at sdk v1.47 (verify, S4a);
- expected (other sets): k6-logged requests × spans per route, measured at
  calibration (SR0 fixes them; health excluded; worker has no self-load);
- agent: `count` connectors keyed by `service.name`,
  `deployment.environment.name` (resource attributes are copied onto the
  output): all spans/logs/datapoints; api entry spans (`kind == SERVER`,
  service = the api, excluding the worker command route) and their error
  subset; health spans (SR8). Output pipeline: `count` → `deltatocumulative`
  → local `prometheus` exporter (`deltatocumulative.max_stale` and the
  exporter's `metric_expiration` both > run length; the exporter alone does not sum the
  connector's non-contiguous deltas — `accumulator.go`) with
  `resource_to_telemetry_conversion: enabled` (else the environment lands in
  `target_info`) and `metric_expiration` > run length; scraped by the checker,
  never sent through the pipeline it audits; no count pipeline feeds another
  `count`; connector version recorded (alpha). Exact OTTL strings
  (`span.kind`-style context paths at 0.160) fixed in S4a;
- **positive control (S4a, every run)**: `telemetrygen` sends exactly N
  spans, log records and data points (and one span with `url.path=/health`)
  through each counted path; every counter must read exactly N (health 1)
  before any "equal" or "0" check is trusted;
- agent: `otelcol_receiver_{accepted,refused}_{spans,log_records,metric_points}`;
- agent→gateway: `otelcol_exporter_{sent,send_failed,enqueue_failed}_*`,
  `otelcol_loadbalancer_backend_outcome`;
- gateway: `otelcol_processor_incoming_items`/`outgoing_items` per processor;
  the traces pipeline is split — `[memory_limiter, filter]` exporting to
  `count/pre` and a `forward` connector, then a pipeline receiving from
  `forward` with `[tail_sampling, resource, batch]` (keeps the SR1 order) —
  giving a **pre**-`tail_sampling` `count` (λ_g per environment) and a
  **post**-`tail_sampling` `count` (retained), same cumulative path,
  `…sampling_trace_dropped_too_early` = 0;
- backends: Tempo `tempo_discarded_spans_total`, Loki
  `loki_discarded_samples_total`, VictoriaMetrics `vm_rows_ignored_total`.
Counters reset on restarts and on the calibration→steady config swap: the
checker always uses `increase()` over the window and scrapes just before any
planned restart (the DNS-mode restore recreates the agent: its queued items
are attributed to that window).
**Justification**: r1's "requests × spans from one calibration request" broke
on self-load, state-dependent branches, dropped health spans and batching;
the tail-sampling counters have no environment label (r2 review).
**When wrong**: a counter is renamed at the pinned version.
**Reversal action**: adapter per version; fail loudly on a missing counter.

### S-D7 — Disk, memory and teardown safety
**Choice**:
- static pre-flight: missing pinned image sizes (`buildx imagetools
  inspect`) + build budget + volume budget (calibrated ingest rate ×
  duration; before the first calibration a conservative constant, recorded)
  + margin, compared with free space **inside the VM** (`df -P /` in a stack
  container started with `compose run --rm`); memory: VM `MemTotal` −
  neighbours' current usage vs the sum of per-service `mem_limit` — initial
  conservative constants per service class (Go app, Python app, agent,
  gateway, Tempo, Loki, VictoriaMetrics, Grafana, k6, checker), recorded,
  replaced after S7; the memory check **hard-fails**;
- `memswap_limit` = `mem_limit` on every service (a swapping leak would
  otherwise flatten the `anon` signal); `memory.swap.current` recorded;
- every service except the canary: `logging: json-file` with `max-size` /
  `max-file` sized to keep the diagnostic lines S-D4 parses for the run;
  container logs are part of the disk budget;
- builder: `docker buildx stop otel-libs-soak` before `up`; never build during
  a run; images built with `--load` exist twice (cache + daemon) — both in the
  build budget;
- continuous disk monitor; VictoriaMetrics `-storage.minFreeDiskSpaceBytes`;
- dedicated buildx builder `otel-libs-soak` (docker-container driver, keep-
  storage cap) — the only project-scoped way to purge build cache; one image
  per language with all apps; BuildKit cache mounts;
- retention: Tempo/Loki 6 h (Loki compactor `retention_enabled`), VM 1 d —
  bounds idle stacks, not runs;
- teardown: EXIT/INT trap (unpause, remove iptables rules, restore DNS per S-D12 `up -d --force-recreate agent-net agent`, `down
  --remove-orphans`); `up --force-recreate`; `purge` = `compose down -v` +
  images with the soak label + `buildx rm otel-libs-soak`; never unscoped
  prune, never `--rmi all` (would remove the Collector image shared with
  `otel-libs-testing`); no shared volumes with `otel-libs-testing`.
**Justification**: Docker disk filled three times on 2026-10-28 on a shared
daemon; the real risk is build cache and images.
**When wrong**: the safeguards block legitimate runs (pre-flight too
conservative) or still let the daemon fill.
**Reversal action**: recalibrate the constants from S7 data (dated note
here); if disk still fills, run the soak on a dedicated Docker context/VM
and drop the shared-daemon assumptions.

### S-D8 — Gateway tail-sampling policy (literal; any-match)
**Choice**: `tail_sampling` evaluates **all** policies; a trace is kept if
**any** policy samples it (no `sample_on_first_match`). Policies:

Calibration (100%) and steady phases use different gateway configs, applied
by restarting the gateways (counters reset; `increase()`). The 0.2 phase
(SR11) uses a third, always-sample config.

1. `errors`: `status_code` ERROR.
2. `debug-nonprod`: `and`[`string_attribute` `debug` = `"true"`;
   `string_attribute` `deployment.environment.name` ∈ {`dev`,`hml`}].
3. `debug-prd`: `and`[`string_attribute` `debug` = `"true"`;
   `string_attribute` env = `prd`; `rate_limiting` `spans_per_second` = R
   (last sub-policy; limit is **per gateway** → effective 2R)].
4. `env-dev`: `and`[env = `dev`; `probabilistic` 100].
5. `env-hml`: `and`[env = `hml`; `probabilistic` 50].
6. `env-prd`: `and`[env = `prd`; `probabilistic` 10].
7. `env-unset`: `and`[`ottl_condition` `resource.attributes["deployment.environment.name"] == nil`;
   `probabilistic` 10] (`ottl_condition` is a valid `and` sub-policy at 0.160,
   `AndSubPolicyCfg` → `sharedPolicyCfg`; `error_mode: ignore`; no
   `invert_match` — deprecated).

Values are lowercase and matched case-sensitively. Expected retained share
(any-match; `and` short-circuits on the first NotSampled sub-policy, so only
debug-prd traces consume rate-limit tokens): `dev` 1; `hml` e + (1−e)·0.5;
`prd` e + (1−e)·0.1; `unset` same as prd, where e = error share of entry
spans (agent `count`). `prd-debug`: `rate_limiting` is a per-span token bucket
per gateway (`rate.NewLimiter(spansPerSecond, burst = 2 × spansPerSecond)`,
`AllowN(now, trace.SpanCount)`), so its output is not a binomial draw and
favors small traces; the post-sampling count also includes prd-debug spans
kept by `env-prd` and `errors`, so the criterion is a bound (S-D5 row), with
2R ≥ max spans per trace (from calibration — otherwise those traces never
pass), per-gateway λ_g measured (not λ/2), and the `usetracestate` feature
gate recorded as off. k6 generates random trace ids (the probabilistic policy
hashes the trace id). `decision_wait` = p99 trace duration (100% calibration)
+ max SDK batch schedule delay + agent batch timeout + export time + margin;
at run time `…sampling_late_span_age` observations with the decision cache
on (`decision_cache.sampled_cache_size` > 0) signal it is too short. The
prd-debug apps use a separate agent port/pipeline so their traces never share
a backend with `prd`.
**Justification**: r1 assumed first-match and would have mis-stated every
rate.
**When wrong**: the platform's real policy (A12) differs.
**Reversal action**: replace `gateway.yaml` with the platform policy and the
formulas with its semantics; re-run. D11.3's "p99 trace duration" input is
corrected accordingly (dated note in config-contract design D11).

### S-D9 — Artifacts under test
**Choice** (repo is public — owner, 2026-10-28):
- Go rc: soak app `go.mod` requires `github.com/staffops/otel-libs/go@v0.3.0-rc.N`
  (no replace); default `GOPROXY`, sumdb on, `GOPRIVATE`/`GONOSUMDB`/
  `GONOPROXY` explicitly empty, `GOTOOLCHAIN=local`;
  report records the helper `go.sum` lines and `go version -m`.
- Python rc: wheel downloaded **on the host** (`gh release download`) into
  `testing/soak/.artifacts/<version>/`, sha256 checked against the release
  digest, installed with `--no-index` (one staging dir per version — never a
  glob over mixed versions); dependencies resolved after; `pip freeze` in
  the report.
- Go builds use a committed `go.sum` and `-mod=readonly` (a mismatch fails);
  `-mod=mod` only with `--from-tree`. `run.sh bump-rc <N>` regenerates the
  committed `go.sum` (network) and shows the diff for review.
- 0.2 apps (SR11): own source trees `testing/soak/apps/{go,python}-v0.2/`
  implementing SR0 against the **0.2 API** (Go gRPC interceptors
  `Unary*/Stream*Interceptor` instead of stats handlers; Python has no
  `shutdown_telemetry` — no last flush, not scored). Go: separate `go.mod`
  (`go 1.22`) requiring only `github.com/staffops/staffops-otel-libs/go
  v0.2.1` (old path, ADR-010; served by proxy and sumdb) + grpc, tidied,
  `go.sum` committed — never sharing a module with the rc apps (MVS would
  lift 0.2's dependencies); built with `golang:1.26`, `GOTOOLCHAIN=local`
  (`unmeasured` whether 2024-era deps build on ≥ 1.23 — S5 builds them
  first; grpc pinned to the 1.67.1 that 0.2.1 requires). Python: the
  `v0.2.0` wheel plus a committed `constraints.txt` pinning each OTel/contrib
  package to its latest release before 2026-07-15 (the `v0.2.0` tag date),
  generated from PyPI upload times or a resolver's exclude-newer option — the
  method recorded (its pyproject has open upper bounds). Env:
  `SERVICE_NAME`, `ENVIRONMENT` (uppercase `PRD`/`DEV`/`HML` — 0.2 emits
  uppercase), `OTEL_EXPORTER_OTLP_ENDPOINT=http://agent:4317` (explicit
  scheme: in 0.2 schemeless means **TLS** — `go/v0.2.1:go/config.go:116-126`,
  `v0.2.0:python/otel_helper/config.py:59-65`) — the listed SR2 exception;
  their traffic goes through the always-sample gateway config (S-D8).
- `--from-tree` builds the rc apps against the working tree for iteration.
- Private-repo fallback (if visibility changes): BuildKit `--ssh default` or
  `--mount=type=secret`; never a token in `ARG`, `ENV`, `COPY` or git config
  persisted in a layer.
**When wrong**: only `--from-tree` validated a fix.
**Reversal action**: acceptance requires the final run on released artifacts.

### S-D10 — Soak-owned apps
**Choice**: `testing/soak/apps/{go,python}/` implementing SR0 — api (HTTP
server + gRPC client), backend (gRPC server), worker (HTTP client, commanded
by the load generator through an api route), canary mode (`otlp,console`).
Same names in both languages; bounded ring buffers; per-request-type log
records at four severities; Python server graceful timeout set.
**Justification**: the `*/example` apps are different domains per language,
lack the gRPC hop in Go, emit no DEBUG, self-generate load and keep unbounded
stores (S0 B1/B2/B4/B5).
**Trade-offs**: more code to maintain; it exercises only the public API.
**When wrong**: soak apps drift from how consumers use the libs.
**Reversal action**: derive them from the examples once the examples meet SR0.
**Alternatives**: patch the examples (they are documentation; changing their
domain breaks docs and dashboards).

### S-D11 — App matrix (reference)
Per language: `prd` api+backend+worker (3); `dev`, `hml`, `unset`,
`prd-debug` api+backend (8); canary api+backend (2) → 13 per language, 26
apps + ~12 infra (≈ 38 containers; memory `unmeasured`, plausibly tight on a
7.65 GiB VM — the pre-flight decides). The gRPC/HTTP/TLS transports are
assigned to `prd`, `dev` and the canary (never dropped). SR6 cases, SR11 0.2
apps and SR13 multi-worker run as **separate phases** (never with the steady
soak). If the pre-flight memory check fails, drop `hml` first, then `unset`;
dropped checks are reported `n/a: dropped`, never passed.

### S-D12 — Failure-injection modes
**Choice**: the agent runs in `network_mode: service:agent-net`, a holder
(alpine + iptables, `NET_ADMIN`) that owns the IP and DNS alias.
**refused** = stop the agent (address kept → RST); **black hole** =
`iptables -I INPUT -p tcp --dport <otlp ports> -j DROP` in the holder;
**hung** = `docker compose pause agent`; **DNS failure + unreachable** =
disconnect the holder, restored with `docker compose -p otel-libs-soak up -d
--force-recreate agent-net agent` (also what the EXIT trap does). The DROP
rule covers every agent receiver port, including the prd-debug and TLS ones. Gateway restarts use `stop/start` on gateway-1 only (traces), so
metrics/logs (gateway-0) are unaffected. Loadbalancer `retry_on_failure`
and `sending_queue` stay off (on, they re-route and split traces).
**Justification**: `docker network disconnect` alone gives fast resolution
failures, not a black hole (S0 H3/H4).
**When wrong**: iptables unavailable in the Docker Desktop VM.
**Reversal action**: toxiproxy `timeout` toxic for the hung/black-hole-like
case, documented as not a SYN black hole.

## Phase K on `k8s01` (added 2026-11-15, r5)

Measurements below were taken on 2026-11-15 with read-only calls against
context `k8s01`: `kubectl get nodes|pods|events|ns|opentelemetrycollector|
mutatingpolicy|validatingpolicies|peerauthentications|authorizationpolicies|
priorityclass|cm obi`, and `kubectl get --raw` on the Prometheus, Tempo and
Loki service proxies. They describe the cluster on that day; the K1 snapshot
re-reads them on every run.

### K-D1 — Use the shared platform pipeline as-is; sampling and routing stay in Phase S
**Choice**: Phase K exports to the platform agent
(`otel-agent-collector.opentelemetry:4317|4318`) and reads the platform
backends. It deploys no Collector of its own. Checks that need a
tail-sampling, load-balanced gateway (SR8 shares, SR9, S-D8, D11.1–D11.3)
are out of Phase K on this cluster.
**Justification**: AGENTS.md principles 2–4 are about the pipeline consumers
actually hit; the platform agent is that pipeline (`k8s_attributes`
enrichment measured, `testing/k8s/README.md` "Measured results"). The
gateway config has neither `tail_sampling` nor a `loadbalancingexporter`
(`otel-gateway` CR, `spec.config.processors` = `memory_limiter`, `batch`).
A namespaced copy would cost ≥ 2 pod slots on a node with 0 free (K-D2).
*(r5 revision, 2026-11-15, after the K0 review: the owner raised kubelet
`maxPods` to 200 (K0b ruling 3), so slots no longer block a copy; the binding
constraint is now CPU and memory requests — ≈ 400–600 m CPU free, K-D2 — and
whether an agent + gateway copy with the platform's requests fits them is
`unmeasured`. The choice stands on the shared-pipeline argument above, not on
slots.)*
**Trade-offs**: the Collector's own counters (`otelcol_*`, jobs
`otel-{agent,gateway}-collector-monitoring`) aggregate every tenant and carry
no `service.name`, so per-service loss accounting (SR4, S-D6) is impossible
here; K6 replaces it with the gateway connector counts. One gateway replica:
its restart resets the connector counters for every tenant.
**When it would be wrong**: the platform adds `tail_sampling` /
`loadbalancingexporter` (then the S-D8 reversal applies: the platform policy
replaces S-D8), or a K result cannot be explained without per-service
Collector counters.
**Reversal action**: deploy a namespaced agent + gateway copy in
`dev-otel-libs` (same CR configs plus `tail_sampling`, `count` connectors and
the S-D6 counting path) once the K-D2 budget fits its CPU and memory requests (and the owner
agrees), or run
those checks on `kind`; record the switch as a dated note here.
**Alternatives**: `kind` on the laptop (no real enrichment, mesh or OBI);
editing the platform gateway (shared and ArgoCD-managed — the owner limited
Phase K to `dev-otel-libs`).

### K-D2 — Resource budget: requests-based pre-flight, one language per window, a platform reserve
*(r5 revision, 2026-11-15, after the K0 review. The first r5 text budgeted
pod slots only; the owner then raised kubelet `maxPods` (K0b ruling 3), which
moved the binding constraint to CPU and memory requests. Rewritten in place —
the r5 draft was not committed.)*
**Choice**: the pre-flight reads the node's `status.allocatable` and Σ
`resources.requests` of its non-terminated pods
(`status.phase!=Succeeded,status.phase!=Failed`, init containers counted the
way the scheduler counts them: max of each init container vs Σ app
containers) and starts the run only when, for CPU and for memory, Σ the
run's pod requests ≤ allocatable − requested − reserve, and the run's pod
count ≤ `allocatable.pods` − non-terminated pods − pod reserve (the slot
check stays as a guard). Every run container sets explicit requests, so the
sum is known before apply. Reserves — proposed initial values for K0b, basis
`unmeasured`: 250 m CPU, 512 Mi memory, 1 pod. One language runs per window
(rotation) unless the budget fits more; the load generator runs in its own
pod by default (`split` layout: one container per pod), so the load → api
and api → backend hops cross ztunnel and every pod is single-container
(K-D4). The compact layout (`testing/k8s/examples`) stays an option, not a
rule. A watch (every 15 s, an initial value) scales the run to 0 on any
`FailedScheduling` event outside `dev-otel-libs` that is newer than the run
start, for a pod still `Pending`, whose message contains
`Too many pods`, `Insufficient cpu` or `Insufficient memory` (SR15).
**Justification**: measured 2026-11-15 by the K0 review: `allocatable.pods`
200 with 117 non-terminated pods; CPU requests 7395 m of 8 allocatable
(≈ 600 m free); memory requests 27008 Mi of ≈ 31.2 Gi (≈ 5 Gi free).
Re-read the same day for this revision (`kubectl describe node k8s01`):
7595 m CPU and 27840 Mi memory requested — the margin moves between reads,
so the pre-flight reads it on every run. The scheduler admits a pod by its
requests, so after the `maxPods` raise the next platform `FailedScheduling`
would read `Insufficient cpu`, not `Too many pods`; the earlier evidence
still shows the failure is real (at 19:31 UTC, 110 of 110 slots, a Velero
maintenance Job Pending with `Too many pods`). All pods have priority 0
(only the two `system-*` PriorityClasses exist): the scheduler reported `No
preemption victims found`, so a starved platform Job just waits. Rotation is
kept because ≈ 400–600 m CPU must cover a whole language stack plus load;
concurrent languages are allowed only when the pre-flight fits them. The
in-pod load generator and the compact layout were slot savings; with slots
free they save nothing (requests are per container, not per pod), and they
cost `k8s.container.name` (K-D4) and a hop that bypasses ztunnel.
**Trade-offs**: small requests (to fit ≈ 400–600 m) make CPU throttling
likely if limits are set and contention likely if not — the report records
`container_cpu_cfs_throttled_periods_total` per container (series present:
44 for `dev-otel-libs` over one day, measured 2026-11-15) and app latency is
read against it; whether throttling distorts SR3 latency is `unmeasured`. A
request-based budget does not see real usage: a run within its requests can
still compete for unrequested CPU (node limits are overcommitted, 209 % CPU
measured). Rotation means one window per language, so SR10-style parity
compares windows with identical load parameters, not concurrent runs. A
separate load pod adds one slot and the ztunnel hop to the client-side
latency.
**When it would be wrong**: a platform `FailedScheduling` appears during a
run with the reserves respected (reserves too small); the run's containers
are throttled enough to fail SR3; or the node's requests move so much that
no window fits (then the run never starts).
**Reversal action**: raise the reserves (dated note); size requests from K1
measurements of the apps' usage; ask the platform owner for a
negative-value PriorityClass for test pods so platform pods preempt them (a
cluster-scoped object — owner decision, K0b); or move the load generator
back into the api pod (compact layout) only if slots become the constraint
again.
**Alternatives**: load from outside the cluster through the `public` Istio
Gateway (needs routes in ArgoCD-managed config, LAN latency in the data);
pod-slot budget only (the first r5 text — blind to the CPU/memory constraint
now binding); a ResourceQuota on `dev-otel-libs` (a namespaced object the
run could own, but it caps the run, not the free margin, so it does not stop
a run from taking the last free CPU).

### K-D3 — Checker: extend `testing/k8s/verify.py`, read-only through the API server proxy
**Choice**: new `verify.py` modes `platform` (K1), `k8s` (K2), `identity`
(K3), `mesh` (K4), `labels` (K5), `connectors` (K6), `soak` (K8), each with
one PASS/FAIL line per check, reusing its `Backends._get` (`kubectl get
--raw`, 60 s timeout). Queries:
- Kubernetes API: node `status.allocatable` (`cpu`, `memory`, `pods`); pods
  with `status.phase!=Succeeded,status.phase!=Failed` and their requests;
  events with reason `FailedScheduling` in every namespace (K-D2 watch);
  terminated containers' `state.terminated.finishedAt` (K3); namespace labels; the `OpenTelemetryCollector` CRs
  `otel-agent`, `otel-gateway`, `otel-logs` (every `auth` key and every
  authenticator extension removed before anything is written or printed);
  MutatingPolicy `namespace-ambient-mode`; every
  `gateways.gateway.networking.k8s.io` — the K4 pre-flight fails only on a
  Gateway of class `istio-waypoint` bound to `dev-otel-libs` (in the
  namespace, or named by its `istio.io/use-waypoint` label); every other
  Gateway (e.g. `generic-gw` in `test`, class `istio`) is recorded in the K1
  snapshot, not judged; `telemetries.telemetry.istio.io`, the `istio` ConfigMap
  `mesh` key (tracing providers).
- Prometheus `monitoring/kps-prometheus:9090` `/api/v1/query[_range]`:
  `target_info{k8s_namespace_name="dev-otel-libs"}`;
  `increase(traces_span_metrics_calls_total{job=~"dev-otel-libs/.+"}[w])` by
  `job`, `span_name`, `status_code`; `traces_service_graph_request_total` /
  `_failed_total` by `client`, `server`; `otelcol_receiver_{accepted,refused}_
  spans`, `otelcol_exporter_{sent,send_failed}_*`, `otelcol_exporter_queue_
  size`, `otelcol_processor_filter_spans.filtered`,
  `otelcol_connector_servicegraph_{total,expired}_edges` (present, measured;
  aggregate only); `container_memory_rss`, `container_memory_working_set_
  bytes`, `container_threads`, `container_cpu_cfs_throttled_periods_total`,
  `kube_pod_container_status_restarts_total` (present for `dev-otel-libs`,
  measured 2026-11-15 over one day: 44 throttling series, 59
  `container_threads` and 59 `container_memory_rss` series, 33 restart
  series); for K3 `max_over_time(timestamp(<metric>{instance="<old>"})
  [w:<step>])` or a raw range selector (`<metric>{instance="<old>"}[w]`) —
  never `timestamp(last_over_time(…))`, which returns the evaluation time
  (measured); for K6 raw counter values at two checkpoints (SR15), not
  `increase()`. `container_file_descriptors` returned no series for the namespace
  (measured), so FDs have no source.
- Tempo `tempo:3200`: `/api/search` (TraceQL on `resource.service.name` and
  the run tag) and `/api/traces/<id>`.
- Loki `loki:3100`: `/loki/api/v1/query_range`, `/series`, `/labels`. Two
  paths write the apps' logs to Loki — the helper's OTLP records
  (`service_name` = `OTEL_SERVICE_NAME`, with `service_instance_id`) and
  `otel-logs` `file_log` of container stdout (`service_name` = the
  Deployment name, with `k8s_container_name`) — measured on the `dotnet-stack`
  streams; counts SHALL select the OTLP streams by `service_instance_id` and
  count records summed across every matching stream (Loki shards a stream by
  the `__stream_shard__` label), never the number of streams.
- Container stdout (load-generator log, K7 console output): `kubectl logs`.
**Justification**: `verify.py` already works this way (32/32 smoke checks,
`testing/k8s/README.md`), needs only the kube context, and writes nothing.
New code K1 adds to `verify.py` (it has neither today): a Prometheus
`/api/v1/query_range` client (only `prom_query` and `prom_series` exist; its
`query_range` call is Loki's) and a read-only `kubectl get -o json` wrapper
for Kubernetes objects (today every call is `kubectl get --raw` through a
service proxy).
**Trade-offs**: every query goes through the API server; proxy limits on
long range queries and response size are `unmeasured`. The service-graph
metric is produced by the gateway `service_graph` connector (its series carry
`client_k8s_cluster_name`, the connector's `k8s.cluster.name` dimension) —
`testing/k8s/README.md` attributed it to Tempo's metrics-generator
(corrected 2026-11-15 in this revision; Tempo runs no `metrics_generator`).
**When it would be wrong**: proxy timeouts or truncated answers on soak-length
windows, or the kube context loses `services/proxy` rights.
**Reversal action**: split range queries by step and window; if still
failing, run the checker as a Job in `dev-otel-libs` that calls the Services
directly (one pod, its requests counted in K-D2).
**Alternatives**: the Grafana API (a token to manage); `kubectl port-forward`
(a `pods/portforward` write, long-lived sessions).

### K-D4 — `k8s.container.name`: asserted in single-container pods only
**Choice**: K2 asserts `k8s.container.name` for single-container pods (the
smoke Jobs, the K3 single-process Deployment); in compact pods its absence is
an expected, reported difference.
**Justification**: `k8s_attributes` v0.165.0 README: "If a pod contains more
than one container, either the `container.id`, or the `k8s.container.name`
attribute must be provided"; the helpers set neither (principle 4) and the
agent's `pod_association` uses `k8s.pod.uid`, `k8s.pod.ip`, `connection`.
Measured: smoke pods get `k8s.container.name=smoke`, the three-container
example pods get none (`testing/k8s/README.md`).
**Trade-offs**: compact-pod metrics lack `k8s_container_name`; series of
api and backend in one pod differ only by `job`/`instance`.
**When it would be wrong**: consumers run multi-container pods and rely on
the container dimension.
**Reversal action**: add to the D11.4 chart checklist a Downward-API value
`OTEL_RESOURCE_ATTRIBUTES=…,k8s.container.name=<name>` set by the chart (a
chart-level attribute — an owner ruling on principle 4 first), or propose a
contract change for an SDK container-id detector; then assert it in every
pod.
**Alternatives**: `split` layout everywhere (r5 first text: "does not fit
the slot budget"). *(r5 revision, 2026-11-15: after the owner raised
`maxPods` (K0b ruling 3) it fits the slot guard, and K-D2 now makes `split`
the default, so K2 can assert `k8s.container.name` in every run pod; the
choice above still governs any compact pod a run uses.)*

### K-D5 — Istio ambient: measure the state the namespace is in, switch only `dev-otel-libs` to `none` and back
**Choice**: K4 runs once in `ambient` (the current state) and once after
`kubectl label ns dev-otel-libs istio.io/dataplane-mode=none --overwrite`;
the run trap restores `istio.io/dataplane-mode=ambient --overwrite`. This
label is SR15's one named cluster-scoped write (owner ruling K0b(2)); the
trap sets it back and never removes it — on a missing label Kyverno
`namespace-ambient-mode` would re-add `ambient` anyway. The
pre-flight refuses K4 when a waypoint, a mesh tracing provider or a
`Telemetry` object exists (SR15).
**Justification**: the namespace is already enrolled (label set by MutatingPolicy
`namespace-ambient-mode`, pods `ambient.istio.io/redirection=enabled`), so
every K result so far was taken under ambient — K4 isolates its effect. The
policy's `needs-label` match condition only fires when the label is absent,
so an explicit `none` is not re-mutated (policy spec read 2026-11-15). The
switch touches no shared object: no `PeerAuthentication` exists (mesh default
PERMISSIVE), so plaintext from a `none` pod to the ambient agent is accepted;
the agent's namespace keeps its own label. ztunnel is L4 and emits no spans;
ParentBased (ADR-002) is unaffected because nothing in the path rewrites
`traceparent` without a waypoint.
**Trade-offs**: whether ambient re-redirects running pods on the label change
without a restart is `unmeasured` on Istio `1.30.8` — K4 restarts the pods in
each mode and checks the pod annotation; a platform-side `PeerAuthentication`
STRICT added later would make the `none` phase lose telemetry (the pre-flight
re-checks it).
**When it would be wrong**: a waypoint or mesh tracing is enabled for the
namespace (then D11.5 governs), or the label switch is reverted by ArgoCD or
Kyverno during the run (the pre-flight and the trap re-read the label).
**Reversal action**: `kubectl label ns dev-otel-libs
istio.io/dataplane-mode=ambient --overwrite` (also the trap), restart the
namespace's pods, re-read `ambient.istio.io/redirection`; if the platform
forbids opting out, record K4 as `n/a: platform` and keep only the ambient
measurements.
**Alternatives**: enrolling a waypoint to test L7 mesh tracing (mesh-wide
tracing provider in ArgoCD-managed `istiod` config — out of scope, SR15);
not testing (every K result would carry an unstated mesh condition).

### K-D6 — Health-span absence proven app-side
**Choice**: K7 runs one canary container exporting traces `otlp,console`
under `/health` probes and counts health spans in its stdout with
`kubectl logs`; Tempo is not used for this check.
**Justification**: the agent's `filter/noise` drops spans whose `url.path` or
`http.route` matches `^/(ping|health|healthz|ready|readyz|live|livez|metrics)$`
before any backend (agent CR), and `filter/noise` has no per-service counter
(`otelcol_processor_filter_spans.filtered` is aggregate) — a helper regression
would be invisible.
**Trade-offs**: the canary's stdout also reaches Loki through `otel-logs`
`file_log` (shared Loki volume: `unmeasured`); the canary therefore runs at a
low fixed rate for a short window.
**When it would be wrong**: the helper's console output format changes, or
the platform drops `filter/noise` (then Tempo absence becomes meaningful).
**Reversal action**: switch K7 to the Tempo query once the agent no longer
filters health paths; otherwise keep the console canary.
**Alternatives**: SDK self-observability span counters (not in every
language).

### K-D7 — OBI comparison is an opt-in experiment in an owner-created `staffops*` namespace
**Choice**: K9 runs only after the owner creates a `staffops*` namespace for it
(OBI's include rule is `namespace_glob: staffops*`); it first checks that OBI
adds no span to helper-instrumented processes, then, optionally, compares an
uninstrumented twin's OBI spans with the helper's spans for the same routes.
**Justification**: OBI's config (`cm obi`, `match_order: first_match_wins`)
excludes processes matching `exports_otlp: {port: 4317, protocol: protobuf}`
before the include rule; whether that matches helper apps exporting
`http/protobuf` to 4318 is `unmeasured` — the duplicate-span risk is real and
specific to helper users on this platform. `dev-otel-libs` stays outside OBI
so every other K check sees SDK telemetry only.
**Trade-offs**: needs a namespace (a cluster-scoped write the owner does);
its pods share the K-D2 budget; results do not gate any release.
**When it would be wrong**: OBI's include rule changes to cover
`dev-otel-libs`.
**Reversal action**: delete the experiment's workloads; the owner deletes the
namespace; if OBI ever covers `dev-otel-libs`, K2–K8 add an OBI-span check
(`telemetry.sdk.name` per span) before trusting counts.
**Alternatives**: label-based OBI selection in `dev-otel-libs` (OBI config
change, platform-owned).

### K-D8 — Cluster failure modes only by namespaced means
**Choice**: K10 injects only: DNS failure (endpoint host that does not
resolve), a Service with no endpoints, and an egress-deny NetworkPolicy in
`dev-otel-libs`. Stopping, pausing or filtering the shared agent is never
done.
**Justification**: the agent serves every namespace; SR15 scope.
**Trade-offs**: which failure the no-endpoint Service and the NetworkPolicy
produce under ambient (RST, timeout, or nothing, since ztunnel carries the
traffic over HBONE port 15008) is `unmeasured`; K10 measures it first and
labels each mode by what was observed, never by intent.
**When it would be wrong**: a mode yields no failure at all (the export still
succeeds) — then it is reported as `not injected`, not as a pass.
**Reversal action**: delete the NetworkPolicy / Service (the run trap does);
keep the full SR5 set in Phase S.
**Alternatives**: toxiproxy as a sidecar in the app pod (no slot, an extra
container image; a later option).

### K-D9 — Prometheus label checks replace the VictoriaMetrics row (K5)
**Choice**: K5 checks the label set the cluster's Prometheus produces from
OTLP: `translation_strategy: UnderscoreEscapingWithSuffixes`,
`keep_identifying_resource_attributes: true`,
`promote_resource_attributes` = `service.name`, `service.namespace`,
`service.version`, `k8s.{cluster,namespace,pod,container,deployment,
statefulset,daemonset,node}.name` (Prometheus CR `kps`, `spec.otlp`, and
`/api/v1/status/config`).
**Justification**: measured series: `job="dev-otel-libs/e2e-go-k8s-http"`,
`instance` = the helper's UUID, `k8s_*` labels on `target_info`.
`deployment.environment.name` is not promoted, so per-environment queries need
a `target_info` join — a difference from D11.6 (VictoriaMetrics promotes every
resource attribute) that the K11 report states for consumers. Loki, by
contrast, has `deployment_environment_name` and `service_instance_id` as
stream labels (measured `/labels`): at least one stream per process (Loki
may shard it further by `__stream_shard__`), so churn per restart is
streams × shards — `unmeasured`.
**Trade-offs**: K5 evidence is Prometheus-specific; D11.6's
`-opentelemetry.ignoreResourceAttributes` advice has no equivalent to check.
**When it would be wrong**: the owner names VictoriaMetrics as the reference
platform (K0b) and the homelab is not representative.
**Reversal action**: keep K5 as homelab evidence, mark it non-representative,
and keep the VictoriaMetrics checks in Phase S (S-D2).
**Alternatives**: none on this cluster without a platform change.

## Report format (reference)
`reports/<UTC>/report.md` + `report.json`: header (rc, commit, image digests,
`go version -m`, `pip freeze`, parameters, VM resources, neighbour aggregate
load — counts and totals only), calibration outputs, per-scenario verdict
with criterion, values and exact queries, disk/memory usage, SR11 table.
