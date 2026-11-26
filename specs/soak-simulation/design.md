# Soak simulation — design

**Status**: Draft r4 (2026-10-28). Requirements: [requirements.md](requirements.md).
r1 superseded after the S0 review (see the requirements r2 note).
r5 (2026-11-15, draft, not yet reviewed — K0): Phase K on `k8s01`, decisions
K-D1…K-D9 at the end; dated notes on S-D1 and S-D2.
r6 (2026-11-19, draft, not yet reviewed — K0c): Phase S dropped (owner);
Phase K carries the D10 evidence. New decisions K-D10…K-D16 (Collector copy,
workload, counting, platform guards, failure modes, orchestration and
evidence, artifact and durations); dated r6 notes on S-D1…S-D12 and
K-D1…K-D9.
r6.1 (2026-11-20): owner rulings on r6 (tasks.md K0d) — loaded-window worker
at 10800 s, no Tempo subtraction (K-D11, K-D12); runtime DNS mode removed and
a burst mode added (K-D14); floors accepted (K-D13); K4 and K7 promoted
(K-D5, K-D6). r6 was never committed: edited in place, each change marked
"r6.1, 2026-11-20".
r6.2 "lean soak" (2026-11-23): owner ruling after the two K0c reviews
BLOCKED r6.1 — scope cut to the guard, the passive window, one loaded window
counted in bounds on the fixed span families, four failure modes, K-shut, K3
and K5 (new K-D17 scope, K-D18 mesh and platform guard, K-D19 counting in
bounds); K4, K7, K8c, the burst and per-severity counts deferred again,
the black hole dropped; dated r6.2 notes on K-D5, K-D6, K-D11…K-D15. Not yet
reviewed (task K0e). Each change is marked "r6.2, 2026-11-23".

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

> *Amended 2026-11-19 (r6):* Phase S is dropped (owner ruling 1). S-D1…S-D12
> stay as written for the record; each carries a one-line r6 note saying
> what, if anything, Phase K keeps from it.

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
*(r6, 2026-11-19: dropped with Phase S; Kubernetes is the only phase.)*

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
*(r6, 2026-11-19: ruled — the homelab Prometheus is the reference;
VictoriaMetrics is accepted gap AG4.)*

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
*(r6, 2026-11-19: Phase K durations are K-D16 — a ≥ 24 h passive window and
a 2 h loaded window, owner ruling 4.)*

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
*(r6, 2026-11-19: dropped; the cluster checker is K-D3 and the run loop K-D15.)*

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
*(r6, 2026-11-19: Phase K keeps the memory, thread, restart, request-impact,
export-timeout and binomial-tolerance rows, on cluster sources (SR15 "Soak
rules", K-D12); the Compose sweep, the sampling and `prd-debug` rows (AG2) and
the host run-validity row (replaced by K-D13 and K-D15) are dropped.)*
*(Recalibrated 2026-11-23, r6.2, for K8a — evidence: a read-only pre-soak
analysis through the Prometheus MCP over 78.9 h of nine demo processes that
never restarted (images `3f36074`, .NET `8e19973`), 2026-11-20 06:43Z →
2026-11-23 13:40Z. RSS rose in steps over the first 6–12 h, then went flat:
second-half slopes 0–0.08 Mi/h, thread counts constant — no leak in any
container. With a 1 h warm-up the ratio rule above still FAILED six
containers (last 15 min / first 15 min: dotnet-worker 1.246, dotnet-backend
1.156, python-api 1.128, go-worker 1.116, python-worker 1.114,
python-backend 1.111) because of those early steps, while the slope rule
passed everywhere. **Change for K8a**: warm-up 6 h, then 18 h judged (the
window stays 24 h); the verdict is the slope rule (≤ 1 % of the median per
hour over the judged span) **and** the same slope rule over the judged
span's second half; the 1.10 ratio is reported, not judged. Trade-off: a
leak that only steps (flat between steps) and keeps stepping after 6 h is
caught by the second-half slope only if a step lands in that half. **When
wrong**: a container still steps after 6 h of a valid window. **Reversal
action**: extend K8a to 30 h with a 12 h warm-up, and judge the ratio again;
record dated here.)*
*(Owner ruling K0e-1, 2026-11-23, supersedes the "Change for K8a" above: K8a
is **30 h = 12 h warm-up + 18 h judged** — the steps ran out to 12 h, so a
6 h warm-up could leave a step in the judged span. The verdict stays the
slope rule over the judged span and its second half, the 1.10 ratio
report-only. Owner ruling K0e-2: the Threads / FDs row has a cluster source
again — `kubectl exec … sh -c 'ls /proc/1/fd | wc -l'` per demo container
per sweep, a named read-only soak-run read (SR15 "Checker"), all four
languages. **Reversal action**: drop the exec read and judge FDs from
`process_open_file_descriptor_count` (Python) only.)*
*(Owner rulings 2026-11-24 and 2026-11-25, supersede the 30 h of K0e-1: K8a
is **12 h = 90 min warm-up + 10 h 30 min judged**, same verdict rules. Run
`k8a-20261124b` (2026-11-24T15:16:59Z → 2026-11-25T03:16:59Z) passed every
container, but the "When wrong" above fired: node-worker RSS stepped until
about 7 h (full-span slope 0.89 of its limit, flat at 58.4 MB after) and
dotnet-worker until about 8 h, inside the judged span. The owner accepted the
PASS on 2026-11-25 with the reduced sensitivity: a leak that steps less often
than every 5 h 15 min (the second half) can pass, and settling steps in the
judged span narrow the full-span margin. node-worker, dotnet-worker, the
dotnet-api thread pool and python-backend threads are on the watch list of the
K11 summary. **Reversal action**: a container fails or ends above 0.9 of a
limit in a later window, or a release needs the SR4 leak claim → re-run K8a at
30 h = 12 h + 18 h, record dated here.)*
*(Owner ruling 2026-11-23, after the K-count code review — integer gauges,
`unmeasured`: for threads, goroutines, FDs and the .NET thread-pool count and
queue length, when every sample of the span is an integer, the slope limit
per judged span or half is max(1 %/h × median, Q / span_h) with Q = 2 units
(span_h = 18 h for the judged span, 9 h for its second half); the queue
length is fitted on 15 min medians. Q supersedes the 1-unit floor first used
for a median of 0. Motivation: a live read of 2026-11-23 had a .NET thread
count with median 5 (limit 0.05/h), so a single one-thread step in a 9 h half
would fail. A single +1 step passes; two steps, or a sustained +1 every 6 h
or less, fail. Also from the review: the passive verdict is INVALID
(`platform: telemetry gap`) when a judged series has fewer than 95 % of its
expected samples in any hour or in total — runtime series at the export
interval (10 s), cAdvisor at its scrape interval (15 s, 242 samples per hour
read 2026-11-23), FDs as successful FD samples per guard read that ran in
that hour (re-review HIGH-2: the loop's period is not exactly 60 s; it now
keeps a fixed cadence, sleeping the interval minus the read's own time, and
hours without reads are left to the wall-clock heartbeat rule). *(Corrected
2026-11-25, K8a review of run `k8a-20261124b`: "any hour" counted
`round(span / 1 h)` whole hours, so a 10 h 30 min judged span left its last
30 min out of the check; the bins are now the whole hours from the judged
start plus a partial last bin when 10 min or more remain (read with an
instant `count_over_time` at the end, its expected count scaled to its
length); a shorter remainder is read the same way and folded into the last
hour, whose expected count grows by it; whole-hour range points never fall in
the tail bin, and a span under 1 h issues no range query. The coverage shares are
written for every judged rule, PASS included, so the artefact proves the
95 %.)* The FD read runs without a
shell (`ls -1 /proc/1/fd`, lines counted). **Reversal action**: set Q from the
K8a noise floor of the first valid run.)*

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
*(r6, 2026-11-19: the counting path — `count` → `deltatocumulative` → a local
`prometheus` exporter, never fed back into the audited pipeline, and the
`telemetrygen` positive control — is what the copy `soak-agent` runs
(K-D10); the gateway pre/post-sampling counts are dropped (AG2).)*

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
*(r6, 2026-11-19: dropped; the cluster disk and memory guards are K-D13.)*

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
*(r6, 2026-11-19: not checked — accepted gap AG2.)*

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
*(Deviation 2026-11-19, r6, owner ruling 3: this round soaks the branch HEAD,
not a released rc — the demo images are built from the commit under test and
their digests recorded (K-D16). The reversal action above does not apply this
round; the D10 amendment of the same date bounds what may change between the
soaked commit and `0.3.0`.)*

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
*(r6, 2026-11-19: superseded — the demo apps of `specs/demo-environment/`
are the workload (K-D11); they exist since 2026-11-16 in all four languages.)*

### S-D11 — App matrix (reference)
Per language: `prd` api+backend+worker (3); `dev`, `hml`, `unset`,
`prd-debug` api+backend (8); canary api+backend (2) → 13 per language, 26
apps + ~12 infra (≈ 38 containers; memory `unmeasured`, plausibly tight on a
7.65 GiB VM — the pre-flight decides). The gRPC/HTTP/TLS transports are
assigned to `prd`, `dev` and the canary (never dropped). SR6 cases, SR11 0.2
apps and SR13 multi-worker run as **separate phases** (never with the steady
soak). If the pre-flight memory check fails, drop `hml` first, then `unset`;
dropped checks are reported `n/a: dropped`, never passed.
*(r6, 2026-11-19: superseded by the demo layout — 12 apps + PostgreSQL +
Redis, `dev` only; the other sets are accepted gap AG5.)*

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
*(r6, 2026-11-19: superseded by K-D14 on the cluster.)*

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
*(Reversed 2026-11-19, r6, owner ruling 5: a dedicated agent-only copy
`soak-agent` now runs in `dev-otel-libs`, deployed by the platform owner's
GitOps session — K-D10. The "no Collector of its own" choice no longer holds;
the gateway half of the reversal (a namespaced gateway with `tail_sampling`)
was not taken: accepted gap AG2.)*

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
*(r6, 2026-11-19: read during r6 — 4970 m CPU and 25 456 Mi memory
requested of 8 / 31 941 Mi, 130 pods of 200 (≈ 3.0 cores free). The default
is now all four languages in one window when the pre-flight fits (as the
demo's DE-O8 ruling); rotation is a pre-flight outcome. The pre-flight and
watch are the existing `testing/k8s/demo/preflight.py` and `schedwatch.py`,
run with the soak-run pods' requests added.)*
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
*(Corrected 2026-11-19, r6: stale — `verify.py` now has
`Backends.prom_query_range` and the read-only `kubectl_json` (added for the
demo, DE5), plus `scan_secret` and `loki_hygiene`. K1 and the new `soak`
mode reuse them; no new client is needed.)*
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
*(r6, 2026-11-19: K4 deferred; the results carry the ambient condition,
accepted gap AG10.)*
*(r6.1, 2026-11-20: AG10 not accepted (owner) — K4 is promoted and runs in
its own window after K10, the demo apps scaled to 0 while the label is
switched and restarted in each mode.)*
*(r6.2, 2026-11-23, lean soak: AG10 accepted (owner) — K4 is deferred again;
the decision above stays for when it is promoted. The trap keeps a guarded
label restore, K-D15 r6.2 note.)*
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
*(r6, 2026-11-19: K7 deferred; health-span absence is accepted gap AG9.)*
*(r6.1, 2026-11-20: AG9 not accepted (owner) — K7 is promoted and runs as
chosen above, before the passive window.)*
*(r6.2, 2026-11-23, lean soak: AG9 accepted (owner) — K7 is deferred again.)*

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
*(r6, 2026-11-19: K9 deferred, optional.)*

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
*(r6, 2026-11-19: superseded by K-D14 — the copy and the soak-run Services
make DNS, refused, hung, back-pressure and redirect modes possible without
touching the shared agent; the NetworkPolicy stays as the black-hole mode.)*

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
*(Corrected 2026-11-19, r6: the promoted list changed — `kps`
`spec.otlp.promoteResourceAttributes` now holds 13 attributes: `cluster`,
`service.name`, `service.namespace`, `service.version`,
`deployment.environment.name`, and `k8s.{cluster,namespace,pod,container,
deployment,statefulset,daemonset,node}.name` (read 2026-11-19). The
`target_info` join above is no longer needed. K1 reads the list on every run
and K5 asserts against what it read. The owner ruled Prometheus the reference
(AG4), so the "when it would be wrong" case is closed for this round.)*

## Phase K r6 (added 2026-11-19)

Facts about the copy come from the platform owner's session that deployed it
(relayed by the coordinator, 2026-11-19); cluster reads are read-only
`kubectl` calls made during r6 on 2026-11-19 (node requests, Prometheus
`spec.otlp`, gateway CR connectors, backend images).

```
k6 Job (soak-run) ─HTTP POST /orders─▶ demo-<lang>-api ─gRPC─▶ demo-<lang>-backend ─▶ demo-postgres
                                            │ XADD                         (all four languages, dev)
demo-<lang>-worker ◀── XREADGROUP (untraced) ┘   worker: DEMO_INTERVAL_SECONDS 5 (K8a) | 10800 (K8b)
   every demo app ─OTLP/gRPC, plaintext─▶ soak-otlp (ExternalName, soak-run; switched in K10)
                                            ▼
 soak-agent (dev-otel-libs, GitOps): otlp → memory_limiter → k8s_attributes(passthrough) → batch
     → otlp_grpc otel-agent-collector.opentelemetry:4317        + count → delta_to_cumulative → :8889 (local)
                                            ▼
 shared otel-agent → otel-gateway (span_metrics, service_graph) → Tempo 3.1.1 · Loki 3.7.10 · Prometheus 3.16.0
 checker (laptop): kubectl get / get --raw service proxy only; run loop under caffeinate (K-D15)
```
*(r6.2, 2026-11-23: in K10 `soak-otlp` points at a `testing/fakeotlp` pod
for *hung* (`--hang`) and *redirect*; no NetworkPolicy is created. The run
loop lives in `testing/k8s/soak/` (task K-loop).)*

### K-D10 — A dedicated agent-only Collector copy `soak-agent` in `dev-otel-libs`
**Choice**: the demo apps export to `soak-agent` (through the soak-run
ExternalName `soak-otlp`, K-D14). As delivered by the platform session: a
plain Deployment, ConfigMap, Service and PodMonitor (not an
`OpenTelemetryCollector` CR), GitOps-managed (Argo CD app `soak-agent`),
`otel/opentelemetry-collector-contrib:0.165.0` by digest, 1 replica, request
100 m / 256 Mi, limit 512 Mi, `GOMEMLIMIT` 450 MiB; labels
`app.kubernetes.io/name=soak-agent`, `app.kubernetes.io/part-of=otel-libs`,
`otel-libs/role=soak-agent`; Service `:4317` (gRPC) and `:4318` (HTTP),
plaintext. Pipelines per signal: `otlp` → `memory_limiter` → `k8s_attributes`
(passthrough: stamps only `k8s.pod.ip`) → `batch` → `otlp_grpc` to
`otel-agent-collector.opentelemetry:4317`, plus `count` → `delta_to_cumulative`
→ `prometheus` exporter on `:8889`, never forwarded. Prometheus job
`dev-otel-libs/soak-agent` (PodMonitor, 15 s): counts
`otel_libs_soak_{spans,span_events,datapoints,log_records}_total` by
`service_name`, `deployment_environment_name` (the sender's job in
`exported_job`); own telemetry at level `detailed` (receiver
accepted/refused/failed, `otelcol_processor_memory_limiter_*`, exporter
sent/send_failed, queue size and capacity, `otelcol_process_memory_rss`,
`otelcol_process_uptime`). Back-pressure on/off is a request to the platform
session (`memory_limiter` 64 / 16 MiB on, 400 / 100 off); each flip rolls the
pod about 1–2 min after the push. Smoke at delivery: `telemetrygen` 10 spans
→ 10 counted, 10 accepted, 10 sent.
**Justification, strongest first**: (1) the shared agent's and gateway's
counters aggregate every tenant and carry no `service.name` (K-D1
trade-off), so the r5 plan could not count per service or account for loss;
the copy carries only the run's apps, so its exporter counters are the run's
own and its `count` metrics are exact per service. (2) It makes failure modes
possible without touching shared Collectors (K-D14): refused and hung on
the app → copy hop (r6.1, 2026-11-20: DNS moved to start-up only), back-pressure in the copy. (3) Passthrough
`k8s_attributes` keeps the platform enrichment honest: the shared agent's
`pod_association` resolves `k8s.pod.ip` to the demo pod instead of the
copy's connection IP. (4) The count path follows S-D6 (cumulative, local,
positive-controlled), so the copy never counts through the pipeline it audits.
**Trade-offs**: one more hop (an extra batch delay in every quiesce wait); a
copy restart resets its counters (handled as a reset, K-D12) and closes the
apps' connections; the copy is agent-only, so gateway tail sampling and
trace-ID routing stay unchecked (AG2); plaintext only (AG1); the count metrics
reach Prometheus through a scrape, so a missed scrape is a gap the reader must
detect; passthrough relies on the connection's source IP, which ambient
preserves for in-mesh pod traffic — `unmeasured` here, checked by K-copy.
**When it would be wrong**: K-copy finds the demo telemetry enriched with the
copy's pod; the copy drops or delays items under the K-cal rate (its own
sentinels fire); or the platform owner withdraws the Argo CD app.
**Reversal action**: point the apps back at the shared agent (hook default),
drop the per-service count checks to report-only end-to-end `span_metrics`
reads, and record the switch dated here; if enrichment is wrong, ask the
platform session to set `pod_association` from `k8s.pod.ip` explicitly or
stamp `k8s.pod.name` from the Downward API.
**Alternatives**: the r5 plan (shared pipeline only — no exact counts, no
injection); an `OpenTelemetryCollector` CR (operator-managed, CR changes go
through the platform anyway; the session chose plain objects); a namespaced
agent + gateway copy with `tail_sampling` (more requests; the owner limited
the copy to an agent).
*(Updated 2026-11-23, soak-agent 1f56009 + b4c1dae: component names are the
non-deprecated `k8s_attributes` and `delta_to_cumulative`, behavior
unchanged; `count` also emits `otel_libs_soak_fixed_spans_total` (K-D19),
with `service.instance.id` defaulted to `"unset"`.)*

### K-D11 — Workload: the demo apps, k6 in a pod, the worker as consumer
**Choice**: the demo's twelve apps (four languages × worker, api, backend)
with the shared PostgreSQL and Redis, deployed by `demo.sh apply` with the
env-override hook (demo DE-D7 amendment 2026-11-19) setting
`OTEL_EXPORTER_OTLP_ENDPOINT=http://soak-otlp:4317`, the canary header, and
`DEMO_INTERVAL_SECONDS` per window. Passive window: worker at 5 s (the demo's
own rate, DE-D7). Loaded window: worker at 10800 s (consumer only; r6.1,
2026-11-20, owner ruling 2 — was 3600 s), the window placed between two
drives (SR15 "Windows"); a k6 Job
(`grafana/k6` from docker.io by digest, `constant-arrival-rate`,
`activeDeadlineSeconds`, explicit requests) drives `POST /orders` on every
api with the demo body (`order_id` bounded as DE-D7 r3.4, `customer` over
three values, `amount` ≤ 1000 except `business_error` at 5000, `scenario`),
a fixed scenario mix and a fixed inbound-class mix. `handleSummary` writes
one JSON with counters per language × route × scenario × status × class;
pass/fail of the k6 run itself is `dropped_iterations` = 0.
**Justification**: the demo apps already meet SR0's intent in all four
languages, with ADR-014 error routes, correlated logs, a queue and DB
clients — more than the never-built soak apps (S2); k6 is the open-loop
generator SR3 asked for; a 10800 s worker interval keeps the queue consumed
(CONSUMER spans exist) while no drive falls inside a 2 h window, so the
exact count needs no Tempo read.
*(r6.1, 2026-11-20: the r6 text used 3600 s and subtracted the one or two
worker-driven traces found in Tempo; the owner set 10800 s and removed the
subtraction.)*
**Trade-offs**: the loaded window must be placed between two drives (the
worker container's start time bounds it; a worker restart inside the window
invalidates it); the demo is `dev`-only (AG5, accepted); POST is the only
route k6 drives (health spans are K7's job); the summary carries no
per-request timing, so p99 is k6's trend percentile.
**When it would be wrong**: a language's worker drives at start or on a
schedule the K-cal read did not see, so a drive lands inside the window; or
the scenario mix gives too few error requests per language for the binomial
tolerance.
**Reversal action**: raise `DEMO_INTERVAL_SECONDS` further or restart the
worker just before t1 (its first drive then comes one interval later); raise
the error shares or the rate after K-cal.
**Alternatives**: the worker as the only load at a short interval (closed
loop, not SR3); soak-owned apps (S2, never built, dropped); k6 outside the
cluster (LAN latency, no route without platform changes, K-D2).
*(r6.2, 2026-11-23, after K0c: (1) corrected — only Go drives at start +
interval; Python, Node.js and .NET drive immediately once their api answers
(SR15 "Windows and drain (r6.2)", code lines there), so the "When it would be
wrong" case above is the normal case for three languages and t1 follows the
observed first `demo.drive`; (2) the worker's drain (`COUNT 10` per 2 s poll,
about 5 msg/s per language, `unmeasured`) caps the loaded rate × the enqueuing
share, or the hook lowers `DEMO_POLL_SECONDS`; t2 waits for a stable consumer
count, and send = process is checked; (3) a roll is forced by
`DEMO_RESTART_NONCE` and scaling is `demo.sh scale` (demo task DE13); the
hook denies `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`. **Reversal
action** for (2): raise the poll rate or lower the loaded rate.)*

### K-D12 — Counting: copy counts as raw samples, sentinels, a gateway-restart detector
**Choice**: per window, the checker reads at checkpoint t2 the raw samples
of `otel_libs_soak_spans_total` and `otel_libs_soak_log_records_total`
over [t1, t2] (`<metric>{job="dev-otel-libs/soak-agent"}[<w>s]` through
`prom_query`), sums positive differences per series, adds a lower value as a
reset, and invalidates the window on a gap > 2 × 15 s while load ran. t1 is
taken after a quiesce before k6 starts, t2 after k6 ends plus a quiesce of
SDK batch delay + copy batch + one scrape interval (+ for end-to-end reads:
agent and gateway batch + `metrics_flush_interval` 30 s). The same rules
read `traces_span_metrics_calls_total` end to end, summing series that
differ only by `service.version`. Expected = Σ (route × scenario × class)
k6-answered × K-cal spans (logs: records) per service; DUPLICATES and LOSS
are separate verdicts. Sentinels per SR15. Gateway-restart detector: the
gateway pod's name and `status.startTime` (`kubectl_json`) at t1 and t2 plus
`otelcol_process_uptime` of job `otel-gateway-collector-monitoring` (exact
exposed name read at K1) growing without a drop over the range.
**Justification**: `span_metrics` expires an idle series after 5 m and keys
on `service.version` (gateway CR, read 2026-11-19), so r5's two instant
reads lose counts on rare cells and across rebuilds; raw samples see every
reset. The copy's counts are cumulative after `deltatocumulative` and local,
so they do not expire with traffic gaps on the gateway. The gateway is a
Deployment: a rollout changes the pod name, so a restart counter per pod
would miss it; pod identity plus uptime catches both a container restart and
a rollout. An absent counter is ambiguous (never incremented, or not
scraped), hence the sibling-success rule.
**Trade-offs**: exactness depends on K-cal's spans per request being stable
per (route × scenario × class) — a branch the calibration did not see breaks
the equality (it is then a finding to explain, not a tolerance to widen);
copy counts are what the apps sent, so app-internal drops before export show
only as LOSS against k6; the end-to-end check depends on a shared gateway
that may restart for platform reasons (window invalid, not failed).
**When it would be wrong**: the copy's counts disagree with its own
`otelcol_receiver_accepted_*` at a clean window (then the count connector,
not the libs, is suspect), or raw range reads time out through the proxy.
**Reversal action**: compare against `otelcol_receiver_accepted_*` of the copy
filtered to the run's window (aggregate but run-only); split range reads by
window (K-D3 reversal).
**Alternatives**: `increase()` (extrapolates, never exact); Tempo or Loki
counts as the primary (Tempo search is paged and eventually consistent;
Loki counts stay as a cross-check).
*(Corrected 2026-11-23, r6.2: the Justification sentence "the copy's counts
… do not expire with traffic gaps" is false. The copy's ConfigMap sets
`deltatocumulative: {}` and its `prometheus/count` exporter has no
`metric_expiration` (read 2026-11-23, `kubectl get cm soak-agent`); upstream
defaults are `max_stale: 5m` and `metric_expiration: 5m`, so an idle copy
series disappears after 5 min and comes back from a new value, like the
gateway's. Also corrected: the otelcol counter names carry no `_total`
suffix, and the gateway-restart detector reads `restartCount` and
`state.running.startedAt`, not `status.startTime` (a container restart keeps
the pod's start time). The counting rules are replaced by K-D19; sentinels
by SR15 "Sentinels (r6.2)".)*
*(Corrected 2026-11-23, K0e review B1, H2, H5: the copy's expiry is now
30 m (`max_stale` and `metric_expiration`, live 2026-11-23), the gateway's
stays 5 m. Sentinels are scoped by verdict: the copy's `count` connector
counts before export, so the copy's own sentinels gate the copy verdicts;
the shared agent and gateway sentinels gate only the end-to-end checks
(gateway `span_metrics` cross-check, SR9, SR10, service graph). K1 reads a
24 h baseline of every sentinel and guard counter, and each window starts
only after a clean 30 min lookback. Every shared-job query carries
`cluster="k8s01"`. **Reversal action**: if the shared sentinels are clean
on every window, gate the copy verdicts on them too.)*

> **Corrected 2026-11-26 (K8b `k8b-20261125`, owner ruling "accept and fix").**
> In `soak.py run`, t1 is the k6 window start, right after the k6 apply. The
> k6 window now lasts until the Job is done (bounded by its deadline), not
> until `--duration`. t2 = the window end (the first guard read after the
> Job succeeded) + `--quiesce`, both on the laptop's clock (default
> `QUIESCE_S`, 120 s; shorter only with `--allow-short-quiesce`). The Job's
> `completionTime` and its skew from the window end are recorded only as
> information, because they come from the cluster's clock. `--hook --rate`
> requires `--worker-interval`; without it the workers keep their 5 s app
> default. Through the quiesce the demo stays hooked, read as
> a guarded loaded window `<window>-q`; a breach or a gap there invalidates
> the count inputs. The trap restores the demo only after t2. The run writes
> `count-window.json` (t1, t2, `k6_end` and its source, `valid`, `reasons`,
> and `count_args`, which is null when there are reasons). With `--hook` it
> also writes `workers.json`: the worker record at the window start, taken
> after guard (g), and again at t2. `verify.py soak count --workers` uses the
> t2 record for the restart and replacement check; the live-pod fallback is
> named in `worker_source`. The worker rules (t1 rule, next drive) are also
> checked at minute 0, with t1 = now (the conservative side) and the planned
> t2 = now + the Job deadline + one loop interval + the read budget +
> quiesce. A problem refuses the window before k6 starts. The next-drive rule keeps t2 + quiesce with t2 already
> holding a quiesce, which is conservative. Cause: in `k8b-20261125` the trap
> restored the demo 9 s after k6 ended. The restored pods' normal traffic
> reached the gateway inside (t1, t2], and the cross-check read DUPLICATES
> (+14 `POST /orders`, +10 send / process in every language). A diagnostic
> re-read with t2 = k6 end + 16 s matched every family exactly. The copy
> verdicts were not affected, because the restored pods do not export to the
> copy. The worker inputs had been built after the trap, from the replaced
> pods.

### K-D13 — Platform guards with dated initial floors
**Choice**: every 60 s the run loop reads the guard set of SR15 "Platform
guards" (shared agent and gateway refused / failed / send-failed /
enqueue-failed and `memory_limiter` refusals, Tempo and Loki discards, node
free memory and idle CPU share, `node_filesystem_avail_bytes{mountpoint="/"}`)
and acts on the first breach. Initial floors, dated 2026-11-19, basis
`unmeasured` (accepted by the owner 2026-11-20, ruling 4), to be replaced
from K1 and K-cal reads: node
`MemAvailable` ≥ 2 GiB, idle CPU ≥ 15 % over 5 min, root filesystem free ≥
10 % of its size.
**Justification**: the run shares one node and one pipeline with every
platform workload; requests-based admission (K-D2) does not see real use, and
a counter rise at the shared Collectors is the earliest sign the run hurts
others. Both reviews asked for it.
**Trade-offs**: a platform incident unrelated to the run invalidates a window
(cost: a re-run, never a false pass); floors are guesses until measured; a
60 s interval lets a burst run one interval longer.
**When it would be wrong**: guards fire on most windows with no run-caused
signal (floors too high or a noisy neighbour), or a platform degradation
starts between reads and is missed.
**Reversal action**: recalibrate the floors from K1/K-cal reads (dated note
here); shorten the interval; if neighbour noise dominates, ask the owner for a
quiet time slot.
**Alternatives**: no guards (both reviews BLOCKED on it); only the
`FailedScheduling` watch (sees admission, not use).
*(r6.2, 2026-11-23: the mesh and platform guard (K-D18) runs with these
guards at every read. The floors are no longer guesses carried to K-cal: K1
records MemAvailable, idle CPU and root filesystem free space for 1 h and
sets the floors from that reading (dated note in the summary); K1 also
records the Prometheus PVC (reported 20Gi bound vs 60Gi requested).)*

*(K-loop code review, 2026-11-23, for owner review — two dated rules:)*
*(1) Floor rule (review H7), replacing "sets the floors from that reading"
above: the initial floors (MemAvailable 2 GiB, idle CPU 15 %, root fs free
10 %) are the lower bound; a K1 1 h minimum below an initial floor is
`BELOW_INITIAL`, a FAIL of the snapshot; a stricter floor may come only from a
run footprint K-cal measures, as observed_min − projected footprint × margin
(1.5), and only when that is above the initial floor; until K-cal the floors
are the initial ones. The loop reads floors only from a passing K1 snapshot no
older than 48 h. Reversal action: a guard that fires with no run-caused signal
is recalibrated from the K-cal footprint, dated here.*
*(2) Shared sentinels in a window (owner ruling M13, 2026-11-23): a rise of a
shared agent / gateway / Tempo / Loki sentinel during a loaded window is a
breach (the load may hurt the shared pipeline: k6 stops, the window is
invalid); during a passive window it is a finding that invalidates only the
end-to-end checks (K0e B1 scoping). At the entry gate every sentinel must be
clean over the lookback.*

### K-D14 — Failure modes on the app → copy hop, by soak-run objects
**Choice**: the apps always export to `soak-otlp`, a soak-run ExternalName
Service whose `externalName` is `soak-agent.dev-otel-libs.svc.cluster.local`.
Modes (SR15 "Failure modes"): refused — `soak-otlp` recreated as a
selector-less ClusterIP Service; hung — a soak-run sink pod (a pinned image
already used by the repo, image recorded) whose listener accepts and never
reads; burst with a hung peer — the hung mode plus a k6 burst above the SDK
batch queue (r6.1, 2026-11-20: AG7 not accepted); back-
pressure — the platform session's flip (the pod roll is part of the mode);
redirect — a soak-run pod answering 307 with a token-carrying `Location`, one
language at a time on `http/protobuf` through the hook; black hole — an
egress NetworkPolicy selecting the pods of the languages under test. Each
mode is labelled by what the apps observed, never by intent.
*(r6.1, 2026-11-20, owner ruling 3: the runtime DNS mode (ExternalName to a
`.invalid` name plus a copy roll) is removed; DNS failure is checked only at
start-up, SR6 "Misconfiguration". Consequence for the remaining switch modes:
established connections to the copy survive a change of `soak-otlp`, so
after the refused and hung switches the apps of the languages under test are
restarted through the hook with unchanged values, and those modes start at
process start; resume is measured after `soak-otlp` is switched back,
without a restart — the clients' own reconnect re-resolves it.)*
**Justification**: the indirection lets a mode end without restarting the
apps (resume is measurable) and without touching any shared object.
**Trade-offs**: the refused and hung modes begin with fresh processes (the
restart needed to drop the established connection), so they measure "peer
failing from the first export" and recovery, not a peer that fails mid-run;
only back-pressure and the black hole act on live connections. What a
no-endpoint Service and a NetworkPolicy produce under ambient (RST, timeout,
nothing) is `unmeasured`; the redirect mode changes protocol and restarts one
language's apps.
**When it would be wrong**: a mode never yields an export failure (then
`not injected`), or the owner wants a peer that fails mid-run for refused
and hung.
**Reversal action**: delete the soak-run objects (the trap does) and restore
`soak-otlp`; for a mid-run refused or hung peer, request a copy roll from the
platform session right after the switch (as the back-pressure flip), so live
clients reconnect into the failure.
**Alternatives**: toxiproxy sidecar (K-D8 alternative); stopping the shared
agent (forbidden, SR15).
*(r6.2, 2026-11-23, lean soak: modes are refused, hung, back-pressure and
redirect. Hung: `testing/fakeotlp` gains a `--hang` mode (handshake done,
Export blocked; task K-hang), built from `testing/fakeotlp/Dockerfile` with
the builder and platform flags of `testing/k8s/build-images.sh` and pushed
to `harbor.staffops.space/development/otel-libs-fakeotlp:<tag>` by digest;
the same image serves the redirect mode through its existing
`/parity-redirect-c/` path. The burst mode is deferred (AG7) and the black
hole dropped (AG11). The back-pressure mode window runs from the new copy
pod Ready plus the first `memory_limiter` refusal to the next copy pod
Ready; the 1–2 min roll delay is `unmeasured`. Criterion 4 is the
failure-line interval proxy and the resume bound starts from upstream
backoff, SR15 "Failure modes (r6.2)".)*

### K-D15 — Orchestration from the operator's laptop; evidence in a committed summary
**Choice**: one run loop on the laptop (`caffeinate`; heartbeat each guard
interval; a wall-clock gap > 3 × the interval invalidates the active window;
a fully blind guard read deletes the k6 Job). An EXIT / INT / TERM trap deletes
objects labelled `otel-libs/soak-run=<run>` only and then re-applies the demo
apps without the soak values. Evidence: `specs/soak-simulation/reports/<run>.md`
committed per run (SR15 "Evidence"); raw output under a git-ignored path.
**Justification**: the owner ruled the loop stays on the laptop (no cluster
writes for a controller); the demo apps carry `otel-libs/run` too, so a
separate `soak-run` label keeps the trap from deleting them; a committed,
reviewed summary is what D10 can link.
**Trade-offs**: a 24 h (30 h, K0e-1) passive window on a laptop risks sleep, network loss
and kube-API outages (windows invalidated, not passed); raw output is not in
git, so a later reviewer has the summary only.
*(Corrected 2026-11-24, after run `k8a-20261123a` ended on one API
connection reset: "fully blind" is judged after the reader's transport
retries — reset, refused, timeout, TLS handshake or an HTTP 5xx from the
API server, its proxy or the backend, two more attempts after 1 s and 3 s
while the pause plus 5 s fits inside the read's deadline, recorded as
`retries` / `retry_error` in `guard.jsonl`. A look-up still unanswered after
them is blind as before; an answer is never retried (SR15 Orchestration,
same date). Implemented in `testing/k8s/soakplatform.py` `Reader.retrying`
and `testing/k8s/soak/soak.py` `KubeReader`.)*
**When it would be wrong**: the passive window is invalidated repeatedly by
laptop gaps.
**Reversal action**: run the loop as a Job in `dev-otel-libs` with a
read-only ServiceAccount (needs a Role that the owner creates) and keep the
trap logic as a finalizer step; record dated here.
**Alternatives**: a CronJob controller in-cluster (cluster writes for RBAC);
no committed evidence (D10 could link nothing durable).
*(r6.2, 2026-11-23, after K0c: the loop was described but owned by no
task. It is task K-loop in `testing/k8s/soak/`: the laptop loop, guards and
heartbeat; a trap that first saves `kubectl logs job/<k6>`, then deletes
only `otel-libs/soak-run=<run>` objects, restores the namespace label if
needed and re-applies the demo; `cleanup --run <id>`; a pre-flight that
refuses to start while any soak-run object exists; renderers for every
soak-run object; the k6 script generator; unit tests. `activeDeadlineSeconds`
bounds k6 when the laptop is cut off. Raw output goes to
`testing/k8s/soak/runs/<run>/` (git-ignored); `scan_secret` runs over the
summary with the canary and `parity-redirect-token` before commit. Code
freeze: the soaked commit carries a local tag `soak/<run>` (owner ruling
K0e-4, 2026-11-23; the owner pushes tags) and no change lands
under `go/`, `python/`, `nodejs/packages/` or `dotnet/` from K8a to K11, or
the loaded window is re-run. **Reversal action** for the freeze: re-run the
loaded window on the new commit (K-D16 reversal).)*
*(Corrected 2026-11-23, K0e review M4, M5, L2, L3: the freeze diff runs
over `testing/k8s/demo/demo-tag.sh`'s input list (adds `nodejs/demo/` and the
`nodejs/package*.json` files), and the running image tags must equal
`demo-tag.sh` at `soak/<run>`; a runtime-behavior change re-runs the passive
window too (K-D16 reversal). The K-loop tests prove the k6 reservoir on the
pinned k6 (fallback: a bounded stdout line per request, first 50 per cell
per VU, saved by the trap's `kubectl logs`) and the guarded label no-op. The
FD exec read runs at every 60 s guard read — 12 calls a minute.)*

> **Corrected 2026-11-26 (K8b-x, K-D21).** The `vu.idInTest` reservoir gate
> was global across the per-language scenarios, so Go and Node.js logged no
> record in `k8b-20261125`. The reservoir is now chosen per language scenario
> from `exec.scenario.iterationInTest`. Each scenario logs one round of
> `len(mix) × len(classes)` iterations every stride = max(1, floor(planned
> rounds / reservoir)) rounds, `reservoir` rounds in all (stride 19 at
> 4 req/s for 2 h). The records are spread over the window, with N per cell
> kept. The fixture was re-recorded on the pinned `grafana/k6:2.3.0`.

### K-D16 — Artifact under test and durations
**Choice**: the commit under test is the branch HEAD at run start; the demo
images are built by `build-demo.sh` from that commit (`git archive HEAD`,
tag from `demo-tag.sh`, which equals the newest commit that touched an image
input) and their digests recorded with `docker buildx imagetools inspect`;
the report records HEAD, the tag and the digests. Durations: passive ≥ 24 h (30 h, K0e-1),
loaded 2 h (owner ruling 4); K-cal before the loaded window; K10 windows after
it.
**Justification**: owner ruling 3 — no rc cut this round, so the soak must
name exactly what ran; digests make "what ran" checkable after the tag moves.
24 h covers daily cycles the 2 h S-D3 window did not.
**Trade-offs**: HEAD moves; the D10 rule (only version strings, CHANGELOG and
docs may differ before `0.3.0`) is the guard; `demo-tag.sh` ignores `*.md`, so
a docs-only HEAD reuses the images (intended).
**When it would be wrong**: a code change lands after the soaked commit.
**Reversal action**: re-run the loaded window (and the passive window if
the change touches runtime behavior) on the new HEAD, or cut an rc and soak
it (S-D9).
**Alternatives**: cut an rc first (owner declined this round).

## Phase K r6.2 — lean soak (added 2026-11-23)

The owner's ruling of 2026-11-23 (tasks.md K0d2) follows two K0c reviews
that BLOCKED r6.1 and estimated 19–28 engineering days for it. The decisions
below are new; K-D10…K-D16 stay with dated r6.2 notes.

### K-D17 — Lean scope: guard, passive window, one loaded window in bounds, four modes, shutdown and identity
**Choice**: the run keeps (1) the mesh and platform guard (K-D18); (2) the
≥ 24 h passive window (30 h with a 12 h warm-up, owner ruling K0e-1,
2026-11-23) with the S-D5 leak and thread rules, FDs (four languages by the
named `exec` read, ruling K0e-2; was Python only), Go
goroutines, the .NET thread pool and restarts (Node.js container metrics
only); (3) one 2 h loaded window with k6, counted in bounds on the fixed
span families only (K-D19), every other span and every log count
report-only; (4) the refused, hung, back-pressure and A31 redirect modes;
(5) K-shut, K3 and K5. K4, K7, K8c, the burst mode and the per-severity
counts are deferred (AG10, AG9, AG8, AG7, AG6); the NetworkPolicy black
hole is dropped (AG11); DE14 and its gunicorn dependency are dropped.
**Justification, strongest first**: (1) cost — the K0c estimate for r6.1 was
19–28 days; the lean path is re-estimated at about 6–9 (tasks.md), and the
dropped items are the ones with new infrastructure (gunicorn mode, canary
containers, a namespace toggle with the demo scaled to 0) or no reliable
injection under ambient (black hole); (2) the kept items carry the D10
question — does the 0.3 branch leak, lose or duplicate telemetry over time
and under exporter failure — and each has a verdict a reviewer can refute;
(3) exact equality across every span family cannot hold on this workload
(DB, Redis and client spans vary per request), so a smaller, provable claim
beats a large unprovable one.
**Trade-offs**: per-severity log loss, burst drops, multi-worker identity,
helper health filtering and mesh effects are not soak-checked in this round
(per-request parity cases still cover the helper filter and the D4 rules);
non-fixed span families can lose items without a verdict (they are in the
summary, report-only).
**When it would be wrong**: a release blocker appears in a deferred area (a
user report of health spans, a multi-worker Python deployment, a burst loss),
or the report-only counts show a loss pattern the fixed families do not.
**Reversal action**: promote the deferred row named by its trigger
(tasks.md), sign the gap as covered again, and re-run only that window.
**Alternatives**: r6.1 as ruled on 2026-11-20 (cost above); a passive window
only (no loss accounting under load or failure).

### K-D18 — Mesh and platform guard: a broken mesh invalidates, never fails
**Choice**: the guard of SR15 "Mesh and platform guard (r6.2)" — ztunnel's
`workload_manager_active_proxy_count` against the annotated Running pods,
`istio_tcp_connections_failed_total{reporter="source"}` for the three
namespaces, ztunnel `Connection refused` lines to `:15008`, restart counts of
the path including ztunnel, istio-cni-node, istiod and kube-state-metrics, a
30-minute clean lookback, `up == 1` for the copy and shared Collectors,
demo pods Ready for 10 min, and a non-zero copy count per service within
60 s of the switch — runs in the pre-flight and at every 60 s guard read; a
breach is reason `platform: mesh`.
**Justification**: the istio-cni-node restart of 2026-11-20 left the mesh
half-enrolled for three days (reported with the owner's ruling): pods kept
the redirection annotation while ztunnel did not proxy them, so their
connections to the copy and the shared agent could fail. Under r6.1 that
would have shown as LOSS in the libraries. All the series were present on
2026-11-23 (instant read: `workload_manager_active_proxy_count` and
`istio_tcp_connections_failed_total` under job `istio-system/ztunnel`).
**Trade-offs**: (a) assumes one node (ztunnel counts the workloads of its
node); (c) parses log text whose format can change with Istio; a mesh fault
that matches none of (a)–(h) still reads as a library result, which the
refutation must catch.
**When it would be wrong**: the cluster gains a node (then compare per node),
the guard fires on windows with no mesh fault (a series whose semantics
differ from the reading above), or Istio leaves ambient.
**Reversal action**: compare (a) per node by ztunnel pod; drop (c) if its
pattern proves unstable and rely on (b); record the change dated here.
**Alternatives**: the r6.1 restart-only roll check (missed the 2026-11-20
case); a mesh-off window (K4, deferred).
*(Corrected 2026-11-23, K0e review B2, M1, M2, M6: item (b) judges only
the run's telemetry edges (demo pods → `soak-agent`, `soak-agent` →
`otel-agent-collector`) against a K1-measured, dated per-edge baseline; the
series counts the TCP connections the source-side ztunnel failed to
establish to that destination. Other edges are report-only findings.
Judging every edge of three namespaces would invalidate windows on traffic
the run does not depend on. (d) exempts the two requested back-pressure
flips; (h) applies at window start only; (e), (g), (h) values are initial,
`unmeasured`. **Reversal action**: widen (b) to an edge again if a finding
on it is shown to affect the run's telemetry.)*

*(Note 2026-11-23, K-loop code review, second round:)* guard (a) compares a
scraped ztunnel count with a live pod list, so the implementation tolerates
pods in transition (container start or Ready within 60 s, deleting, finished
within 60 s, gone since the previous read) and breaches only on a mismatch
seen on two consecutive reads. A real (a) fault therefore stops the load
within two guard intervals, not one as SR15 states for the other items; the
trade is no false `platform: mesh` breach from a scrape lag over a 30 h
window. The 30 s scrape interval and the 60 s transition window are initial
values, `unmeasured`. **Reversal action**: if K1/K8a shows the scrape lag is
shorter, drop to a single read with the transition tolerance only.

### K-D19 — Counting in bounds on the fixed span families
**Choice**: the verdicts count only the fixed span families (requirements
Glossary) per `service_name`: from the gateway's
`traces_span_metrics_calls_total` by `span_name` / `span_kind`, and from the
copy's `otel_libs_soak_spans_total` total only for a service whose K-cal
shows the total fixed per cell. Expected is an interval [answered × k,
(answered + no answer) × k]; LOSS below, DUPLICATES above. Raw samples over
(t1, t2] follow the series rules of SR15 "Counting in bounds (r6.2)" (a
series first seen in the window adds its first value; one that reappears
adds v if v < last, else v − last; copy series summed over `pod`,
`instance`, `exported_instance`). t1 is at most 4 min after the last
traffic. K-cal checks integrality and stability per cell over two repeats
and excludes, by name, the families that fail. A copy roll reports loss as
an interval with the unscraped tail.
**Justification**: the K0c reviews showed the r6.1 equality could not hold:
spans other than the fixed families vary per request (retries, pool
connects, Redis commands), the copy series expire after 5 min (K-D12
correction) and come back from a new value, one service is spread over
several series (pod, instance, exported_instance), and k6's no-answer
requests may or may not have produced spans. A bound absorbs only the last
two, which are known; it does not widen for unexplained variation — a family
that K-cal finds non-integral is excluded and named, not tolerated.
**Trade-offs**: loss in non-fixed families is report-only; the family counts
depend on the shared gateway's `span_metrics` (a gateway restart invalidates
the window, K-D12); the copy cannot separate families without a second
count metric.
**When it would be wrong**: K-cal finds no fixed family stable for a language
(then that language has no count verdict), or the gateway series and the
copy total disagree at a clean window.
*(Owner ruling K0e-3, 2026-11-23: the OTTL-filtered `count` metric — fixed
families only, per sending instance — is requested from the platform
session now; family verdicts use it once it exists and fall back to the
gateway's `span_metrics` until then.)*
**Reversal action**: if the filtered metric is not delivered or disagrees
with the gateway at a clean window, judge families from the gateway only; if a language has no stable family, report its
counts only and list it in the summary.
**Alternatives**: the r6.1 exact equality (cannot hold); Tempo trace counts
(paged, eventually consistent, K-D12 alternative).
*(Corrected 2026-11-23, K0e review H1–H4, M3, superseding parts of the
choice above: the copy metric is delivered as
`otel_libs_soak_fixed_spans_total` with `service_instance_id`,
`exported_instance`, `exported_job`, `pod`, `instance`, `endpoint=count` and
**no family label** (smoke: 10 spans, 5 fixed). The verdict is therefore
one fixed total per `service_instance_id` at the copy, gated by the copy's
own sentinels; the gateway per-family counts (summed by `service_name`,
`span_kind`, `span_name` over all other labels) are a cross-check, valid
only with clean shared sentinels; per-family copy counts are not available.
A non-integral K-cal total names its cause and reports the gateway
cross-check. The delivered OTTL condition misses Go's and .NET's gRPC client
spans (`rpc.system.name`, not `rpc.system`); the platform session is asked
to add the `rpc.system.name` clause, and K-copy's control sends a CLIENT
span with each key. Reappearance: a gap ≥ the source's expiry (copy 30 m,
gateway 5 m) is a reset; a shorter gap adds v − last, or v when v < last.
The "t1 ≤ 4 min" constraint is dropped. K-shut reads the old
`service_instance_id` on this metric. **Reversal action**: if the platform
session adds a family label, judge families at the copy and demote the
gateway to report-only.)*
*(Updated 2026-11-23, soak-agent 1f56009 + b4c1dae: H1 closed — the
`rpc.system.name` clause is live (smoke: `rpc.system` 4/4 fixed,
`rpc.system.name` 4/4, neither 2/4, server only). `service.instance.id`
defaults to `"unset"` in the `count` connector, which otherwise drops a span
missing a listed attribute; a demo series with `service_instance_id="unset"`
is a finding (D4) and invalidates that service's per-instance verdict.
Components renamed `k8s_attributes`, `delta_to_cumulative`, behavior
unchanged.)*

### K-D20 — The calibration driver: one cell at a time, four languages per k6 run (added 2026-11-25, task K-cal-d)
**Choice**: `soak.py cal` (`testing/k8s/soak/cal.py`) runs inside `run`'s
pre-flight, entry gate, hook and trap (shared code, not a copy). The hook is
always applied, with the worker interval at 21600 s. Phase A: a quiesce, then
for each of the 12 cells of the K8b plan (`k6script` default mix × classes)
two k6 runs of N = 30 requests per language at 2 req/s, each followed by a
quiesce of 120 s (`soakcount.QUIESCE_S`); one k6 run drives all four
languages. A repeat's delta is cut from one raw read per source at the
quiesced boundaries with K-count's series rules; the repeats are written in
`calibrate()`'s input shape (`kcal-input.json`), then `soak-cal.json`. Worker
first drive: the first copy fixed-count step of the api or worker after the
hook (non-Go); Go `unmeasured` (start + interval). Phase B: steps 2, 5, 10,
20 req/s per language, 300 s each, a step clean when its windows are valid and
`dropped_iterations` = 0; loaded rate = highest clean step minus one step;
drain rate measured only while a backlog is left at a boundary.
**Justification, strongest first**: (1) isolation — neither count source
carries the cell, so the cells of one language cannot share a run, while the
languages can (services `demo-<lang>-*` are disjoint; each api calls its own
backend, `testing/k8s/demo/demo.sh:584-589`); four languages per run divides
phase A's wall clock by four. (2) Reuse — the repeats go through
`calibrate()`, so `verify.py soak cal` on the written input reproduces the
k values, and the exclusions (invalid repeats, a planned cell never run) are
named by `calibrate()` itself (`invalid`, `planned`, added here). (3) Low
rate and N = 30: a fixed family that drops or adds one span in 30 requests
shows as a non-integral k (1/30); 2 req/s keeps VUs (22 per language) and the
copy far from any limit, so the k values measure the helper, not load
effects. (4) Steps 2, 5, 10, 20 bracket the initial drain estimate (about
5 msg/s, SR15 "Windows and drain"; × the `ok` share 0.7 → about 7 req/s) so
the drain rate is measured at the upper steps and the loaded rate has at
least one step below the cap.
**Trade-offs**: 28 k6 runs and 1.9–2.4 h wall clock (estimate from the guard
cadence: a window ends at the first 60 s read after its Job; `unmeasured`); the
first-drive time is an upper bound (one export interval + one copy scrape);
a worker that keeps up at 20 req/s has no measured drain rate (lower bound
only); a backlog from a saturated step is drained before the run ends (at
most 900 s, else recorded).
**When it would be wrong**: the demo gains a cross-language call (then run
one language per k6 run); the quiesce proves too short (a k differs between
repeats with no family varying — raise `--quiesce`); every step is clean (then
add a higher step, since the loaded rate is only "highest minus one").
**Reversal action**: `--langs <one>` per run, `--quiesce`, `--steps`; record
the change dated here.
**Alternatives**: one cell per language per run (×4 wall clock, no isolation
gain); per-cell counts from Tempo (paged, eventually consistent, K-D12).
*(Code review 2026-11-25, APPROVE WITH SUGGESTIONS, applied:)* a step
saturates a worker only when `send` − `process` (upper) > max(10 msgs, 5 % of
`send`) — one late CONSUMER span or a lower/upper ambiguity is not a drain
rate (M1); an absent copy series is 0 only when another service of the
language counted in the same repeat, else the repeat is invalid ("absent copy
series, no sibling success", K-D12 sibling rule) (M2); a gateway family whose
lower and upper counts differ is left out of the calibration input (M3); the
driver writes `k8b_rate` = min(loaded, floor(drain / `ok` share)) per language
and overall, and exits 2 on a `rate_cap` FAIL (SR15 "Windows and drain") (M4);
the worker-interval check uses the worst case (every k6 Job to its
`activeDeadlineSeconds`; 5.2 h at the defaults) and is repeated before each
window (L1); the pre-flight refuses when the top step's k6 memory limit
exceeds MemAvailable minus the K-D13 floor; k6 peak RSS is `unmeasured` (the
summary has no process-memory metric) (L2); the drain-wait reads and each
step's `noanswer` are kept (L3, L4).

> **Corrected 2026-11-26 (K8b `k8b-20261125`).** `calibrate()` records a
> gateway family's k = 0 for a measured cell in which the family was absent
> in every repeat. A measured cell has no invalid repeat, at least two
> repeats, and in each repeat N > 0 and every request answered as expected.
> Each repeat also needs sibling evidence from the gateway read. In a sampled
> cell, some `demo-<lang>-*` family must count above 0. In a class `s00` cell,
> where the unsampled inbound traceparent makes every family read 0, a series
> of that language present at 0 is enough. An empty read (`{}`), or a read
> without that language, gives no k, because it could come from a gateway or
> expiry gap and a k = 0 would then hide a loss. This differs from the copy,
> where an absent series stays ambiguous: for a family, the language's other
> gateway series are the sibling evidence. A residual risk predates this
> rule. The read reaches back the expiry plus margins before t1, so in an
> `s00` cell a series left over from the previous cell reads "present at 0"
> even if the gateway dropped spans. The same holds for the families
> themselves, which got k = 0 before this rule through the equal-integral
> path. The shared sentinels, read in every window, bound it. An invalid, single or
> unmeasured cell gets no k and stays `missing`, so the check is
> UNQUALIFIED. Cases in `kcal-20261125d`: `producer:send` and
> `consumer:process` in the error scenarios; `rpc-client` and the backend
> families for `unhandled`. `verify.py soak cal --input kcal-input.json
> --output <new>` re-derives a calibration without the cluster. It never
> overwrites the target and writes it atomically with mode 0600. The driver's
> `soak-cal.json` stays as written. For `kcal-20261125d` the re-derived file
> is `soak-cal.r5.json`, with the same k values as the original and the zeros
> added (r2 to r4 were intermediate rules of the same fix; r5 equals them).
> `count` takes `--kcal soak-cal.r5.json`.

### K-D21 — The K8b checks `count` does not cover: `verify.py soak extras` (added 2026-11-26, task K8b-x)
**Choice**: one read-only action, `verify.py soak extras`
(`testing/k8s/soakextras.py`), runs the five remaining K8b checks over the
inputs of `count` (`--t1 --t2 --k6-log --kcal [--k6-end]`, or
`--count-window`; a count window with `valid: false` is refused, exit 2). It
writes `soak-extras.json`, prints one row per item, and exits 0 only when
every judged verdict is PASS (REPORT-ONLY allowed; UNMEASURED, UNQUALIFIED,
FAIL and PREVIEW exit 1). The graph, orphan and parity checks are
end-to-end checks, gated by the shared agent and gateway sentinels the way
`count` gates its cross-check (`scoped_gates`). The gates are extended to
the graph's read time when that read is after t2.
1. **api → backend service graph** (SR15 "Counting": the `failed` share per
   edge within the S-D5 binomial tolerance, on the api → backend edge only).
   - What is read: `traces_service_graph_request_total` (total and failed)
     for the `demo-<lang>-api` → `demo-<lang>-backend` edge, as raw samples
     over (t1, t_read].
   - t_read is the first connector flush after k6's end plus the delays a
     span crosses before it counts: the SDK batch delay (5 s), the agent
     batch timeout, the store TTL, and 2 × the gateway batch timeout. It is
     capped at t2 + settle, and when no flush falls before the cap the
     check is UNMEASURED.
   - The connector flushes every 60 s (`connector.go:59`, collector-contrib
     v0.165.0) and evicts a series after 15 min idle (`connector.go:815-849`;
     it has no `metrics_expiration` field).
   - Judged: only the failed share. p comes from the K-cal backend k over
     the gRPC-failing scenarios; PASS when |p̂ − p| ≤ 3σ. The share is
     UNQUALIFIED when the total reads LOSS, when n < 1000, or when a cell
     has no k.
   - Report-only: the exact total and failed counts, in
     [Σ answered × k, Σ (answered + no answer) × k + w]. w is the
     worker → api edge growth over the window. The lower end is exact, so a
     loss is never hidden. Subtracting w was tried and refuted on the data.
   - Calibration: the nominal k applies only when K-cal recorded
     `qualified: false` (Go, REPORT-ONLY, wide band). A service or family
     absent from the calibration file is UNQUALIFIED.
2. **Secret canary** (SR15 "Secret canary (A29)"). The run's token is read
   by the module only. It is never printed, logged, written or put in a
   query, every error text is redacted before truncation, and the tests use
   a fake token. Each source is a row: PASS when scanned; UNMEASURED when
   capped, incomplete or unreadable. The sources:
   - every Loki stream of `dev-otel-libs` and `opentelemetry` over
     [t1, t2 + settle] — line, labels and structured metadata — plus demo
     records without `k8s_namespace_name`; a namespace with no stream is
     unmeasured;
   - the demo container logs that the trap saves before the restore (new
     step 1b, files 0600, a marker in place of any log with a canary line).
     For a run older than step 1b, the "measured empty via `file_log`"
     inference is REPORT-ONLY, so the check stays UNMEASURED;
   - the Collector containers' logs since t1. Collector stdout does reach
     Loki through `otel-logs` `file_log`;
   - the span values of the fetched traces, the saved k6 log, and
     `soak-extras.json` before it is written.
3. **SR9 orphans** on the k6 reservoir, N = `--orphan-sample` traces per
   cell, recorded.
   - s01, fetched by ID: only the entry span's parent may be missing, and
     it must equal k6's `parent_id`. A missing trace is FAIL, fewer than N
     checked is UNQUALIFIED, and a fetch error is UNMEASURED.
   - Class none, found by a TraceQL search: the entry span must be a root.
   - s00, fetched by ID: the trace must be absent.
   - `gateway.id`: required on every span only when a gateway processor
     really sets it. On `k8s01` none does (one gateway replica), so the
     routing half is AG2.
   - Tempo safety (platform owner, after `tempo-0` was OOMKilled at
     2026-11-26T15:53:35Z by trace-by-ID reads over uncompacted blocks):
     - every Tempo read is strictly sequential, at most `--tempo-rate` per
       minute (default 10);
     - before each batch of 10 reads, the tool reads tempo-0's working set.
       At or above `--tempo-mem-stop` (1.5 GiB), or when that read fails,
       Tempo reads stop and the rest is UNMEASURED;
     - a failed read makes that item UNMEASURED and never aborts the run;
       three consecutive failures stop SR9.
     - *(Corrected 2026-11-26.)* The platform raised Tempo's memory limit
       to 4 GiB (platform commit 085be8f, owner approval), so the default
       `--tempo-mem-stop` is now 3 GiB, 75 % of that limit. The rate limit
       stays at 10 reads per minute.
4. **SR10 parity** across the four languages, per app role.
   - Metrics: names and label keys from `/api/v1/series` over (t1, t2] for
     the demo jobs. Units are compared through the name's unit suffix only.
   - Label keys of a metric are compared only when every language that has
     it had traffic for it in the window; otherwise the item is UNMEASURED.
   - Spans: names and kinds from TraceQL metrics, with `demo:<lang>:`
     normalized.
   - The gate is an explicit allowlist, `testing/k8s/soak/sr10-gaps.json`.
     Each entry names an exact item, the missing languages, the reason and
     the gaps row, plus a status: `proposed`, `needs-row` or `accepted`.
   - A difference passes only on an `accepted` entry, or as a runtime
     metric family owned by every language that has it. `needs-row` (a real
     gap, or one whose gaps row is missing) never passes.
   - `--sr10-accept-proposed` is a preview: the verdict is PREVIEW, never
     PASS.
   - The gaps tables give only a candidate row, printed next to a FAIL.
5. **Per-`severity_number` Loki counts** per demo service (AG6),
   report-only.
**Justification**: these checks need the backends (Tempo, Loki, the service
graph), not the copy, so they sit apart from `count`. An explicit allowlist
replaces a token match, which review round 1 showed gave false PASSes.
**When it would be wrong**: Tempo serves only its live store (about 20 min)
whenever its blocklist poll fails, as it did from 2026-11-25 16:23Z until
the platform fix on 2026-11-26. An accepted entry that is too broad hides a
new difference.
**Reversal action**: run `extras` within Tempo's horizon, right after the
run, when the backend reads fail. Keep allowlist entries exact.

### K-D22 — K-shut, K3 and K5: `soak.py shut`, one deletion inside a hooked window, a pod watch, `verify.py soak identity` and `labels` (added 2026-11-26, task K-shut + K3 + K5)
**Choice**: a separate mode, `soak.py shut --run <id> --rate R
--worker-interval I --kcal <soak-cal.json> [--pre S] [--post P] [--delta δ]`
(`testing/k8s/soak/shut.py`). It shares `run`'s pre-flight, entry gate, hook,
session, trap, loop and quiesce, as K-D20 does. Every api Deployment must
have one replica, or the mode refuses before any write. One k6 Job runs
`phase=pre` (0..S) and `phase=post` (S..S+P) per language.

**The deletion.**
- The loop's `tick`, between guard reads, deletes each api pod once.
- When: at the k6 container's `startedAt` + S + δ, corrected by the
  measured laptop–cluster clock skew (the k6 Job's `creationTimestamp`
  against the midpoint of the apply).
- How: by label and name; kubectl must print `pod "<name>" deleted`.
- The window is invalid when |skew| > δ − graceful stop − 5 s, when the
  delete landed before the post phase started (taken from the k6 log
  timestamps), or when the delete was late by more than one loop interval
  or by more than post − 60 s.
- The old `service_instance_id` is the api instance whose copy count grew
  in (t1, now].
- `deleted_at` is recorded on the cluster clock.

**Logs and pod state.**
- A pod watch child records `finishedAt`, exit code and the deletion time.
  It keeps only state fields, in memory and in `raw/pods.jsonl`, never the
  pod objects, because their env carries the canary header.
- Right before each delete, one `kubectl logs --timestamps` snapshot is
  taken, then a bounded `kubectl logs -f` (grace + 15 s), awaited up to 2 s
  before the delete. The two are merged, and the result saves the dying api
  container's log through the trap step-1b path
  (marker on a canary line, mode 0600, counts only). `verify.py soak
  extras` scans it as its own canary source; a failed save is UNMEASURED.

**Guard (b) at the deletion.** In the shut path only, the `demo->soak-agent`
edge exempts at most 3 `istio_tcp_connections_failed_total` increments,
recorded with their `response_flags`, when all of these hold:
- the increment's `source_workload` is the deleted api;
- its sample time is in [deleted_at, finishedAt + 30 s], or in the
  replacement pod's first 30 s plus one ztunnel scrape.

Every other edge, and `run` and `cal`, stay strict. The exemption also
narrows by `response_flags`: in the deletion window only increments that
are not a `CONNECT` failure, and in the replacement window only `CONNECT`
failures. Every observed flag value is recorded.

It downgrades a breach to a finding only when all of these hold (review
round 2):
- re-running the guard's own judge gives NOISY (UNKNOWN and ABSENT stay
  breaches);
- the re-read covers the guard's own window range (`guard.window_lookback`);
- no increment falls outside the exemption windows;
- the exempted steps equal the counted increase.

The exemptions are recorded per api. When an api's count is REPORT-ONLY
(Go), an exemption in its deletion window makes its shut verdict
UNQUALIFIED ("final flush unverifiable").

**Outputs.**
- `shut-input.json` (`verify.py soak shut`): per api the old instance,
  `deleted_at`, `finishedAt`, exit code, the pod's grace, and an upper
  bound on the post requests issued before `finishedAt`, computed from the
  rate and the measured post start.
- `identity-input.json`, `shut-window.json` and `workers.json`.

**Verdicts.**
- **Exit within grace:** judged in all four languages. Exit 137 is a FAIL:
  a deliberate rule, stricter than "exit code recorded", because SIGKILL
  means the grace ran out. Exit 143 is a finding, shown next to the count
  verdict.
- **The count:** judged for the K-cal-qualified apis (Python, Node.js,
  .NET); Go's is REPORT-ONLY.
- **K3** (`verify.py soak identity`, `testing/k8s/soakident.py`):
  - The hook's own apply is the `DEMO_RESTART_NONCE` roll (SR15 "Restart
    mechanics (r6.2)"). It is not a pure restart: the old pods export to
    the shared agent and the new ones through the copy.
  - Per service, each new pod has exactly one new instance. That instance
    is not an old one, not `unset`, and the same in Prometheus `instance`,
    the copy's `service_instance_id` and Loki `service_instance_id`.
  - Each old instance's last sample, read from its pod's start to
    `finishedAt` + 10 min, is ≤ `finishedAt` + the export interval + 1 s.
    `finishedAt` has one-second resolution, hence the 1 s.
  - Report-only: the last sample ≥ the deletion time, on the cluster clock
    (the watch's `deletionTimestamp`, else `deleted_at` + skew), as the
    shutdown flush signal (SR7); a Loki id that belongs to no recorded pod, and the Tempo
    `resource.service.instance.id` read through `TempoGuard`.
  - The K-shut replacement pods are REPORT-ONLY.
- **K5** (`verify.py soak labels`): on the app-pushed series (those with
  `instance`):
  - `job` = `<service_namespace>/<service_name>`, where `service.namespace`
    is added by the platform's `k8s_attributes`;
  - `instance` = `target_info`'s `service_instance_id`;
  - every promoted attribute, read from `prometheus/kps`
    `promoteResourceAttributes`, is on every series with the same value;
  - no other resource label is on a data series. A leak names its metrics.
  - The series count per metric before and after each roll is recorded. A
    restart makes new series. The gateway span-metrics series (no
    `instance`) are counted apart, not judged.

**Justification**: a `run` flag that deletes pods would let a K8b
invocation do it by mistake. Using the hook's roll avoids a second rollout
inside the count window. A watch catches the terminated status that a poll
can miss. A single-thread tick never races a guard read.

**When it would be wrong**: the watch misses the terminated status, so the
exit check is UNQUALIFIED; a second api pod answers k6; Prometheus stops
putting `k8s_pod_name` on app series, so K3 is UNMEASURED.

**Reversal action**: K3 on a second nonce roll after K-shut; a 1 s poll
beside the watch; a dated note here.

> **Corrected 2026-11-26 (first run `kshut-20261126`, invalid).** The first
> run deleted the four api pods 9 s late and ended invalid. Causes and
> fixes:
> 1. **Guard (c).** ztunnel logged 3 `Connection refused :15008` lines, from
>    k6 to the dying api pods, 0.3–0.6 s after the deletes. Guard (c) gets a
>    shut-only exemption like guard (b)'s. It covers only JSON lines whose
>    source is this window's k6 pod and whose destination is a deleted api
>    pod, both in `dev-otel-libs`, inside [deleted_at, finishedAt + one
>    interval] on the cluster clock, at most 20 lines, each recorded. It
>    downgrades only when every line is exempt.
> 2. **No post line found.** The k6 log timestamps came with the node's
>    offset (`-03:00`), and the parser accepted only `Z`. It now accepts any
>    offset.
> 3. **k6 log.** After the Job was suspended, `kubectl logs` exited 0 with
>    "unable to retrieve container logs", and that text overwrote the
>    usable log. Now an answer without the k6 banner, a reservoir line or
>    the summary goes to `.log.err`, the stop-time save is kept, and the
>    source is recorded.
> 4. **K3 query.** `timestamp()` over an instance's series drops `__name__`,
>    and Prometheus answered 422 ("same labelset"). K3 now reads the raw
>    range `{job, instance}[900s]`, with a `target_info` fallback.
> 5. **Uncaught error.** `__main__.QueryError` escaped `main_soak` as a
>    traceback; a final handler now prints ERROR and exits 1.
> 6. **Deletion time.** The kubelet's final grace-0 delete rewrites
>    `deletionTimestamp`. The request time is now the first value seen
>    minus `deletionGracePeriodSeconds`.
> 7. **Late deletes.** The 9 s came from four 2 s follower waits in a row.
>    They now share one 2 s deadline.
>
> K5 on that run: PASS in 12/12. K3, re-read after fix 4: PASS in 12/12,
> and the shut roll PASS in 4/4 (report-only).

> **Corrected 2026-11-26 (rerun `kshut-20261126b`, refused before its
> window).**
> 1. **Guard (h) refused at the hook.** It summed the copy series per
>    service. The hook came 27 min after the previous run's trap, while the
>    copy keeps an idle series for 30 min, so the old series was still
>    live. It went flat and then expired as the new one was born (for
>    demo-python-worker the sum went 1346 → 6), and every service read "no
>    increase". The guard now applies the series rules per series: a born
>    series adds its value, a reset adds v, an expired one adds nothing.
> 2. **The Go demo worker ignores `DEMO_INTERVAL_SECONDS` above 3600 s.**
>    It falls back to 5 s (`go/demo/internal/demokit/demokit.go:128`, code
>    frozen until K11); Python, Node.js and .NET accept any positive value.
>    So `--worker-interval` 10800 (K8b) and 21600 (K-cal) made Go drive
>    every 5 s. That is the likely cause of Go's non-integral K-cal totals
>    and of its K8b api excess (74 319 vs 69 120).
>
> `run` and `shut` now refuse a hook interval above 3600 s while `go` is in
> `--langs`, and `cal` warns. K-shut runs with `--worker-interval 3600`.

## Report format (reference)
`reports/<UTC>/report.md` + `report.json`: header (rc, commit, image digests,
`go version -m`, `pip freeze`, parameters, VM resources, neighbour aggregate
load — counts and totals only), calibration outputs, per-scenario verdict
with criterion, values and exact queries, disk/memory usage, SR11 table.
*(Amended 2026-11-19, r6: Phase K writes `specs/soak-simulation/reports/<run>.md`
(committed; SR15 "Evidence", K-D15) instead of `reports/<UTC>/report.{md,json}`.)*
