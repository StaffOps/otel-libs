# Soak simulation — design

**Status**: Draft r4 (2026-10-28). Requirements: [requirements.md](requirements.md).
r1 superseded after the S0 review (see the requirements r2 note).

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

### S-D2 — Backends: Tempo, Loki, VictoriaMetrics single-node, Grafana
**Choice**: the platform's backend family, single-node, OTLP ingest.
VictoriaMetrics with Prometheus naming (SR1 flags); Loki with structured
metadata (default ≥ 3.0) and compactor retention.
**Justification**: SR8/SR10/SR11 are claims about stored data and naming.
**Trade-offs**: ~4 more images.
**When wrong**: the platform changes backends.
**Reversal action**: swap the service and its checker adapter.
**Alternatives**: Collector `debug`/`file` only (no naming/label evidence).

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

## Report format (reference)
`reports/<UTC>/report.md` + `report.json`: header (rc, commit, image digests,
`go version -m`, `pip freeze`, parameters, VM resources, neighbour aggregate
load — counts and totals only), calibration outputs, per-scenario verdict
with criterion, values and exact queries, disk/memory usage, SR11 table.
