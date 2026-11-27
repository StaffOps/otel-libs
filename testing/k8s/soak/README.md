# Soak run loop (`testing/k8s/soak/`)

The run loop of the Phase K soak on the homelab cluster `k8s01`
([`specs/soak-simulation/`](../../../specs/soak-simulation/), task K-loop;
SR15 "Orchestration (r6.2)", "Load (r6.2)", "Mesh and platform guard (r6.2)",
"Evidence (r6.2)"; design K-D14, K-D15, K-D17, K-D18). It runs on the
operator's laptop. Python standard library only:

```bash
python3 -I testing/k8s/soak/soak.py <mode> --run <id> [...]
```

| File | What it holds |
|---|---|
| `soak.py` | CLI, the window loop, the trap, `cleanup`, the pre-flight, the dry run, the cluster side of `cal` (`CalOps`) |
| `cal.py` | the K-cal driver (task K-cal-d, design K-D20): the plan, phases A and B, the derivations |
| `shut.py` | the K-shut + K3 + K5 driver (design K-D22 draft): the plan, the pod watch, the deletion tick, the inputs of `verify.py soak shut`, `identity` and `labels` |
| `guard.py` | the guard read: a thin adapter over K1's `testing/k8s/soakplatform.py` (guard (b)–(h), sentinels, edges, `sel`; guard (a), the node-wide ztunnel proxy count, is not read — owner ruling 2026-11-24, the soak judges `dev-otel-libs`, not the cluster) |
| `render.py` | every soak-run object, as JSON |
| `k6script.py` | the k6 script generator and the summary / reservoir parser |
| `fakekube.py` | a fake `kubectl` (and `demo.sh`) for the tests and the dry run |
| `test_*.py`, `fixtures/` | unit tests; a recorded run of the pinned k6 |

## Modes

| Mode | Does |
|---|---|
| `preflight --run <id> [--tag <t>]` | Reads only. Refuses while any object labelled `otel-libs/soak-run` exists (of any run id), an object without that label carries a soak name (`soak-otlp`, `soak-k6-*`, …), or the namespace label `istio.io/dataplane-mode` is not `ambient`; records the running demo (one image tag, the replicas per language, from the live Deployments) and refuses when the tag is not `--tag` or `demo-tag.sh`'s; then the entry gate, one line per item. |
| `run --run <id> --window <name> --duration <s> [--rate <n>] [--langs ...] [--hook] [--control] [--worker-interval <s>] [--quiesce <s> [--allow-short-quiesce]] [--tag <t>]` | The pre-flight and the entry gate first, before any write. Then `soak-otlp` (copy variant); with `--control` (task K-copy), the positive control before the hook: the copy's `soak-control` series read, the `soak-control` Job applied and waited for (360 s), the counts polled until they arrive (120 s) and read again two scrapes (30 s) later; PASS only when the delta is exactly `render.control_expected` (`otel_libs_soak_spans_total` +8; `otel_libs_soak_fixed_spans_total` +2 for `<run>-ctl-a`, `<run>-ctl-b` and `unset`, +1 for `<run>-ctl-n`; an absent series counts 0, a decrease is a copy reset); `control.json`, a `windows.jsonl` record and `raw/soak-control.log`; a FAIL ends the run before the hook (the trap runs); with `--hook`, the demo re-applied through `demo.sh`'s `DEMO_ENV_FILE`, guard (g) polled until the rolled pods are Ready for 10 min (Terminating pods skipped; at most 10 + 10 min), and guard (h) as an increase of every service's copy count since the hook; with `--rate`, the worker window checked (below), the k6 ConfigMap and Job; then a guard read every 60 s until the window ends: with k6, when its Job succeeds (at most its `activeDeadlineSeconds`; `--duration` is k6's own duration), else after `--duration`. A loaded window whose k6 Job succeeded is followed by the quiesce (below). The trap always runs at the end. |
| `cal --run <id> [--langs ...] [--n N] [--rate R] [--steps a,b,c] [--step-duration S] [--quiesce Q] [--worker-interval I] [--drain-wait W] [--control] [--dry-run]` | K-cal (below). The same pre-flight, entry gate, hook (always applied), soak-run labels and trap as `run`. `--dry-run` prints the plan (cells, N, repeats, steps, the wall-clock estimate, the objects) and makes no kubectl call and writes nothing. |
| `shut --run <id> --rate R --worker-interval I --kcal <soak-cal.json> [--langs ...] [--pre S] [--post S] [--delta S] [--quiesce Q] [--control]` | K-shut + K3 + K5 (below). The same pre-flight, entry gate, hook (always applied: the nonce roll), soak-run labels, guarded loop and trap as `run`; one k6 Job with `phase=pre` and `phase=post`; each api pod deleted by name at the boundary + δ. |
| `cleanup --run <id> [--tag <t>]` | The trap's steps, for a run whose laptop was cut off. It restores the demo from `runs/<id>/state.json`; `--tag` forces a restore with that tag (replicas as recorded, else 1). |
| `render --run <id> --what <object>` | Prints one soak-run object (offline). Objects: `k6`, `k6-configmap`, `k6-job`, `soak-otlp --variant copy\|refused\|hung\|redirect`, `sink-hung`, `sink-redirect`, `control`, `fallback --langs <lang>`. |
| `dry-run --run <id>` | Offline, on `fakekube.py`: creates one soak-run object of each kind, runs the trap and checks it deleted exactly those and nothing else (the seed includes a soak-labelled Service in another namespace, which must stay). |

Env: `KUBE_CONTEXT` (`k8s01`; another context is refused unless
`--allow-other-context`), `NAMESPACE` (only `dev-otel-libs` is accepted),
`SOAK_RUNS_DIR`, and `BACKENDS_NAMESPACE` / `PROMETHEUS` as for `verify.py`.
`--kubectl <path>` (or `SOAK_KUBECTL`) replaces `kubectl` for every call, the
guard reads included. Every kubectl call is bounded (25 s for a read) and a
guard read has a deadline of 0.8 × the interval. kubectl and `demo.sh` run in
their own process session; INT, TERM or HUP ends their process groups before
the trap runs.

**The Go worker's interval cap** (run `kshut-20261126b`): the Go demo worker
reads `DEMO_INTERVAL_SECONDS` with `demokit.EnvSeconds`, which falls back to its
5 s default for any value above 3600 (`go/demo/internal/demokit/demokit.go:128`;
Python, Node.js and .NET take any positive value). With `--worker-interval
10800` the Go worker drove every 5 s inside the window. `run` and `shut`
refuse a hook interval above 3600 while `go` is in `--langs`; `cal` warns.
Use `--worker-interval 3600` (a window must then end within an hour of the
workers' start, which the minute-0 worker check enforces).

## Window rules

- **Heartbeat.** `runs/<id>/heartbeat` is written at every guard read. A
  wall-clock gap of more than 3 × the interval (180 s) since the last
  heartbeat, or a clock that went back, invalidates the window: `laptop:
  wall-clock gap`. `caffeinate -dims -w <pid>` keeps macOS awake while the
  loop runs.
- **Guard read (60 s, fixed cadence).** A read starts every 60 s: the loop
  sleeps 60 s minus the read's own time (FD sweep included), and not at all
  after a read longer than that. K1's guard over the last 120 s (twice the interval,
  so a start between two reads is never missed; raw reads reach a further
  `BORN_GRACE` + one scrape back, so a series born in the window counts),
  without (g) and (h), which apply at window start only; every roll-list pod
  of the previous read must still be there with the same `restartCount`, and
  a roll-list container that is not running is a breach; the K-D13 floors —
  the initial 2 GiB / 15 % / 10 %, or stricter ones from a passing K1
  `snapshot.json` no older than 48 h (the loop logs which); the sentinels
  scoped by verdict (K0e B1, owner ruling M13): a shared sentinel rise is a
  breach in a loaded window and a finding in a passive one; a copy sentinel
  rise is a finding (it gates the copy verdicts, judged by K-count). Item (d)
  exempts the copy rolls a window asks for, inside their time bound
  (`exempt_copy_rolls`, `exempt_window`: the two back-pressure flips, K0e M1).
  Failing edges other than the two telemetry edges are findings (B2). The
  FD sweep runs after every read (K-count, owner ruling K0e-2):
  `ls -1 /proc/1/fd` (no shell; the lines are counted) in every Running demo
  container of the run's languages, in parallel, 10 s per call, through
  `Kube.exec_fd` (that fixed command, a demo pod and a DNS-1123 container
  name only); each read's `fd` record in `guard.jsonl` is what `verify.py
  soak passive` judges. A failed exec or an empty listing is recorded as an
  error, never as 0 FDs, and never fatal.
- **Breach.** The window is invalid with the reason `platform: mesh (<item>)`
  (an item of (b)–(h)) or `platform: guard (<item>)`; `kubectl logs job/<k6>`
  is saved to `raw/<job>.stop.log`, then the Job is suspended (its pods go,
  the Job stays for the trap).
- **Blind read** (every look-up failed, or the pod list or the guard itself
  failed): the logs are saved and the k6 Job is deleted, retried with backoff
  (5 s … 60 s) until it succeeds or the Job's `activeDeadlineSeconds` has
  passed. A partly blind read is recorded, not acted on. A look-up has
  failed only after its transport retries: a kubectl call that got no answer
  (connection reset or refused, a timeout, a TLS handshake failure) or a 5xx
  from the API server, its service proxy or the backend behind it is tried
  twice more, after 1 s and 3 s, while the pause plus 5 s fits before the
  window read's deadline (0.8 × the interval); entry reads have no deadline,
  so the count bounds them and a timed-out call is not retried there.
  Each read records `retries` in `guard.jsonl`, with `retry_error` (the last
  transport failure) when it is not 0. An answer is never retried: an empty
  Prometheus result stays a blind item, an error status in a 200 answer or a 4xx fails at
  once (correction 2026-11-24, after run `k8a-20261123a` ended on one reset).
- **k6.** A failed k6 Job makes the window invalid (`k6: Job failed
  (<reason>)`); so does a k6 pod that is not running once the pull allowance
  (300 s) has passed (`ImagePullBackOff` and the like). A succeeded Job ends
  the window. A k6 start that fails is recorded in `windows.jsonl` too.
- **Entry gate.** Before any write: K1's gate over the 30 min lookback (every
  guard and sentinel counter clean, (a)–(g) now) and the floors. A failure
  writes nothing and says to ask the platform session. With `--hook` the gate
  is read again right before the window, after the (g) and (h) waits; a
  breach there refuses the run and the trap restores the demo.
- **Guard (a).** ztunnel's proxied count must lie between the settled
  annotated Running pods and that number plus the pods in transition: a pod
  deleting, with a container not running yet, whose youngest container start
  or Ready transition is under 60 s old, that left Running within 60 s, or
  that the previous read saw and is now gone. A mismatch is a finding on its
  first read and a breach only on the second consecutive read (60 s later);
  the entry gate re-reads (a) once after one interval. The 30 s scrape
  interval behind the 60 s is an initial value (2026-11-23, `unmeasured`).
- **Scrape gaps.** A counter series with no sample before the window's start
  that has a sample in the hour before it only had a scrape gap: its first
  value is not counted and the read item is blind (reset unknown), not an
  increase.

## The quiesce and the count inputs (`run`)

Correction 2026-11-26, after K8b run `k8b-20261125`: the trap restored the
demo 9 s after k6 ended, so the restored pods' own demo traffic reached the
gateway inside (t1, t2] (the cross-check read DUPLICATES of +14 / +10 in
every language), and the workers were read after the trap had replaced them.

- **The k6 window.** It runs until the k6 Job succeeds, at most the Job's
  `activeDeadlineSeconds` (pull + `--duration` + graceful stop + summary), as
  K-cal's k6 windows; `--duration` is k6's own duration, not the window's. Each
  window record carries `ended`: `load done` (the Job succeeded), `duration`
  (the window's time ran out first) or `invalid` (a breach, a gap, a blind read
  or a k6 failure).
- **Quiesce.** After a loaded window whose k6 Job succeeded (`ended` = `load
  done`), `run` keeps the demo as the window left it (hooked) for `--quiesce`
  seconds (default `soakcount.QUIESCE_S`, 120 s), as the window
  `<window>-q`: a guarded window read as loaded (spans are in flight), the
  same as K-cal's quiet windows, with its own `windows.jsonl` record. A
  separate window keeps the k6 window's start and end as they are. A breach
  there, or more than one guard interval + 30 s between the k6 window and the
  quiesce, makes the run exit 1 and the count inputs invalid. No quiesce
  follows an invalid window or one that did not end with `load done`. Only
  then the trap runs. An INT, TERM or HUP during the quiesce still runs the
  trap at once; then neither `count-window.json` nor the t2 workers record is
  written. A `--quiesce` below 120 s needs `--allow-short-quiesce` (`0` = no
  quiesce): `count_args` passes the same value to `count`, which also loosens
  count's t1 rule; `short_quiesce` records it. A non-finite or negative value
  is refused before any read.
- **Worker window, at minute 0** (with `--hook` and `--rate`): before k6
  starts, `soakcount.window_problems` over the start record against the
  planned t2 (now + the Job's deadline + one guard interval + the read
  budget, 0.8 × the interval, since the loop ends at the first read after the
  deadline, + the quiesce), with t1 = now (the window starts a moment later,
  so the t1 rule is checked on the conservative side): a failed start read, a
  Go worker without `startedAt`, another without a first `demo.drive`, t1
  before the t1 rule, or a next drive before t2 + quiesce refuses the run
  (`worker window: ...; not started`) and the trap restores. `--hook
  --rate` requires `--worker-interval` (refused before any read): without it
  the hooked workers keep the app's own `DEMO_INTERVAL_SECONDS`, which
  neither this check nor `count` can know. The
  next-drive rule is `count`'s: next drive > t2 + quiesce, and t2 already
  includes the quiesce, so the window keeps two quiesces clear of the next
  drive (the conservative side; unchanged).
- **`count-window.json`** (when the k6 window started): `t1` (the window
  start, right after the k6 apply), `k6_end` = `window_end` (the loop saw
  the Job done; the laptop clock, as t1), `window_ended`, `t2` = `window_end`
  + `quiesce_s` (laptop clock), `k6_completion_time` (the Job's
  `status.completionTime`, the cluster's clock: information only) and
  `k6_completion_skew_s` (window end − completionTime), `quiesce_window`, `quiesce_end` (≥ `t2`), `short_quiesce`,
  `valid` and `reasons`, `k6_log` (`raw/soak-k6-<window>.log`, saved by the
  trap), `workers`, `worker_interval_s`, and `count_args`, the arguments of
  `verify.py soak count` (paths relative to `runs/<id>/`; add `--kcal`), or
  null when `reasons` is not empty. Reasons: no `--hook` (no `workers.json`),
  an invalid window, `k6 not done at window end`, an invalid quiesce, a
  quiesce that ended before t2. The run exits 1 when the count inputs are
  invalid.
- **`workers.json`** (with `--hook`): `start` = `{at, workers}` written right
  before the window, from `cal.first_drives` over the copy series since the
  hook (pod, `startedAt`, `restartCount`, the first `demo.drive`; Go's is
  `unmeasured`, its t1 rule stays `startedAt` + quiesce); `t2` = `{at,
  workers}` read after the quiesce, before the trap (pod, `startedAt`,
  `restartCount`). A failed read is recorded as `error`, never as workers.
  `verify.py soak count --workers runs/<id>/workers.json` uses `start` for the
  t1 and next-drive rules and `t2` for the restart / replacement check; a flat
  `{lang: {...}}` file, or one without a `t2` record, falls back to the live
  worker pods (stated in `worker_source`; after the trap's restore they read
  as replaced).

## The K8b extra checks (`verify.py soak extras`)

`count` judges the fixed-family counts; `extras` (`testing/k8s/soakextras.py`,
design K-D21 draft) runs the other K8b checks, read-only, and writes
`soak-extras.json` (or `--output`), one line per item as `count` prints, exit 0
only when every judged verdict is PASS (REPORT-ONLY and N/A allowed):

```bash
python3 -I testing/k8s/verify.py soak extras --run <id> \
  --count-window testing/k8s/soak/runs/<id>/count-window.json \
  --kcal testing/k8s/soak/runs/<kcal-run>/soak-cal.json
# or --t1 --t2 --k6-log [--k6-end] instead of --count-window (flags win over the file);
# a count window with valid: false is refused (exit 2); its k6_log is relative to runs/<id>/
```

- **graph** — per language, `traces_service_graph_request_total` on the
  api → backend edge (no `connection_type`), raw samples over (t1, t_read]:
  t_read is the first connector flush (one sample per 60 s push) after k6's end
  (`--k6-end`, the count window's `k6_end`, else t2) + the SDK
  BatchSpanProcessor delay (5 s) + the agent `batch.timeout` + the connector's
  `store.ttl` + 2 × the gateway `batch.timeout` (from the otel-agent and
  gateway CRs), capped at t2 + `--settle` (no flush before the cap:
  UNMEASURED); the shared gates then run to t_read; the series expire after 15 min idle (hard-coded
  in the servicegraph connector, `connector.go:815-849` v0.165.0; it has no
  `metrics_expiration` field). Judged: only the failed share (r6.2 "Counting
  in bounds" judges only the fixed span families). Report-only: the exact
  count (total and failed) against Σ answered × the backend
  `server:demo.v1.OrderService/PlaceOrder` k;
  the upper end adds the no-answer requests × k and the demo-<lang>-worker →
  demo-<lang>-api edge growth over the read (the restored demo's own traffic
  after the trap, at most one backend call per worker request), the lower end
  stays exact. The `failed="true"` share against k6's share
  of gRPC-failing scenarios (`business_error`, `db_error`) within 3 σ,
  σ = √(p(1−p)/n); the share is UNQUALIFIED when the total is LOSS, n < 1000
  (S-D5 floor) or a cell has no k. The nominal k (Go) is used only when K-cal
  recorded `qualified: false` (verdict REPORT-ONLY); a service or family missing
  from the calibration file is UNQUALIFIED. The connector's expired / total
  edge counters are report-only (all edges).
- **Tempo reads** (platform owner's rules, 2026-11-26, after `tempo-0` was
  OOMKilled by parallel trace-by-ID reads): every Tempo read (by-ID fetches,
  searches, the SR10 TraceQL metrics) is strictly sequential, at most
  `--tempo-rate` per minute (default 10); before every batch of 10 reads
  `tempo-0`'s `container_memory_working_set_bytes` is read from Prometheus — at
  or above `--tempo-mem-stop` GiB (default 1.5), or unreadable, no further read
  and the rest is UNMEASURED with the reason; a failed read (ServiceUnavailable,
  EOF, timeout) is that item's UNMEASURED and never aborts the run; 3
  consecutive failures stop every further read. At 10/min a full reservoir
  (48 cells × 50) takes about 4 h: lower `--orphan-sample` for a shorter run.
- **canary** — `runs/<id>/canary`, read by the module only, never printed,
  logged, written or put in a query; every error text is redacted
  (case-insensitive) before it is truncated; hits are named by field, never
  shown. Sources (each a row: PASS when scanned, UNMEASURED when capped,
  incomplete — fewer records read than counted — or unreadable): every Loki
  stream of `dev-otel-libs` and `opentelemetry` over [t1, t2 + `--settle`]
  (line, labels, structured metadata; a namespace without a stream is
  unmeasured), demo-service records without `k8s_namespace_name`; the demo
  container logs the trap saved (step 1b); a hooked run without that record
  is UNMEASURED; only a run not hooked may fall back to "measured empty via
  otel-logs file_log" (REPORT-ONLY, so the check stays UNMEASURED), when `file_log` demonstrably
  collected the namespace in the window (records of other containers without
  `service_instance_id`) and no `otel-logs` container started after t1;
  the current Collector containers' logs since t1 when they ran since t1; the
  span values of the traces the orphan check fetched; the saved k6 log; and
  `soak-extras.json` itself before it is written.
- **orphans** (SR9) — the k6 reservoir (`SOAKRES` lines): N = `--orphan-sample`
  (default 50) traces per cell. s01 by id (one missing parent allowed, the
  entry span's, equal to k6's `parent_id`; a missing trace is a FAIL), s00 by id
  (must be absent), class `none` (k6 knows no trace id) and a language without
  reservoir records by a TraceQL search of api entry spans over ten slices of
  the window, classified by the entry span. A cell with fewer than N checked is
  UNQUALIFIED, a fetch error UNMEASURED. When a gateway processor really sets
  `gateway.id` (an attributes/resource action or an OTTL `set`), every span
  must carry one value; on `k8s01` none does (one gateway replica, no
  load-balancing tier): AG2. When Tempo returns no trace of the window (search
  and five reservoir ids), every language is UNMEASURED.
- **parity** (SR10) — per app role, the demo jobs' metric names and label keys
  (`/api/v1/series` over the window, buckets excluded) and span names and kinds
  (TraceQL metrics plus the fetched traces; span names `demo:go:` … →
  `demo:<lang>:`). Units are compared by the name's unit suffix only (the
  metadata API is global per name; its units are recorded report-only). A
  difference passes on a runtime metric family owned by every language that
  has it (`go_`, `process_` / `system_` / `cpython_`, `dotnet_`, `nodejs_` /
  `v8js_`), or on an `accepted` entry of `testing/k8s/soak/sr10-gaps.json`
  matching kind, role, item (exact or a declared prefix), attribute and
  covering every missing language. Every entry is `proposed` (or `needs-row`:
  a real gap, or one that needs a gaps row first — it never passes) until the
  owner accepts it; `--sr10-accept-proposed` previews the proposed ones: the
  verdict is then PREVIEW, never PASS, and `extras` never exits 0. Label keys
  of a role and metric are compared only when every language that has the
  metric had traffic for it in the window (its counter grew between t1 and t2);
  otherwise the item is UNMEASURED with the reason. A "Known emitted-telemetry
  gaps" row that names the item is printed next to a FAIL as a candidate only.
- **logs** — per `severity_number`, the OTLP records (`service_instance_id`
  set) per demo service, report-only (AG6); compared with soak-cal.json
  only if it carries an expectation (it does not today).

graph, orphans and parity are gated by the shared agent and gateway
sentinels over (t1, t2] (UNQUALIFIED when dirty, the measured verdict kept);
the canary and the log counts are not.

## Calibration (`cal`, task K-cal-d)

The driver of K-cal (design K-D20). Defaults: N = 30 requests per language
and repeat at 2 req/s (15 s), 2 repeats, quiesce 120 s (`soakcount.QUIESCE_S`),
steps 2, 5, 10, 20 req/s per language for 300 s each, the hook's worker
interval 21600 s, a drain wait of at most 900 s.

- **Hook.** As `run --hook` with `*/worker DEMO_INTERVAL_SECONDS` = the
  worker interval: the copy only sees the hooked demo, and no worker drive
  may land inside the calibration: refused at plan time against the worst
  case (every k6 Job to its `activeDeadlineSeconds`; 5.2 h at the defaults),
  stopped before the first k6 run against the recorded worker starts, and
  checked again before each window (now + its worst case + the quiesce).
- **k6 memory.** A pre-flight read, before any write: the top step's k6
  memory limit (256 MiB + 4 MiB per VU) must fit in the node's MemAvailable
  minus the K-D13 floor; a blind read refuses. k6's peak RSS is `unmeasured`
  (the summary carries no process-memory metric).
- **Worker first drive.** Per non-Go worker: the first step of the copy's
  `otel_libs_soak_fixed_spans_total` for its api or worker after the hook (an
  upper bound: the drive precedes it by up to one export interval and one copy
  scrape), with the delay from the container's `startedAt`. Go drives first at
  start + interval, so it is `unmeasured` with that reason.
- **Phase A.** A quiesce, then per cell of the K8b plan (`k6script` default
  mix × classes, 12 cells) two k6 runs of that one cell, each followed by a
  quiesce; the four languages share a k6 run (their services are disjoint and
  both sources carry `service_name`; the cells of one language cannot, no
  source carries the cell). After a cell's second quiesce: one raw read of the
  copy and one of the gateway over the cell, cut at the quiesced boundaries
  with K-count's series rules, and the copy gate (sentinels, queue 0 at both
  ends). Every k6 run and every quiesce is a guarded window (read as loaded:
  their spans are in flight); an invalid one, a summary missing, or a gap of
  more than 3 × the interval between two windows stops the phase. Each k6 Job
  is deleted after its logs are saved to `raw/soak-k6-<window>.log`.
- **`kcal-input.json`** (rewritten atomically after every cell):
  `{"repeats": [...], "invalid": [...], "mix": {...}, "planned": [cell ids],
  "drain": {lang: msg/s}}`. A repeat is `calibrate()`'s
  `{"cell", "counts": {expected, unexpected, noanswer}, "copy": {service:
  delta}, "gateway": {service: {family: delta}}, "unset": [...]}` plus
  `window`, `t1`, `t2` and, when a service had no copy series at all, `absent`
  (recorded as 0 only when another service of the language counted in the
  same repeat; else the repeat is invalid: "absent copy series, no sibling
  success"). A gateway family whose lower and upper counts differ is left out
  of `gateway` and named in `gateway_ambiguous`. A repeat with an invalid
  window, an unclean copy gate or an ambiguous copy count goes to `invalid`
  with its `reasons`; `calibrate()`
  then excludes its cell by name, and a planned cell with no repeat as
  "0 repeat(s)". `verify.py soak cal --input kcal-input.json` reproduces the
  calibration part.
- **`soak-cal.json`**: `calibrate()`'s output (`services`, `families`,
  `excluded_families`, `rate_cap`; what `verify.py soak count --kcal` reads)
  plus `driver`: the plan, the estimate, `workers`, `phase_a` (cells, copy
  gates, `aborted`), `steps` (with `dropped_iterations` and `noanswer`),
  `loaded_rate`, `k8b_rate`, `k8b_rate_per_lang`, `drain`, `drain_wait` (with
  every read), `k6_peak_rss`. Re-running
  `verify.py soak cal` overwrites it without `driver`; `--output <path>`
  writes elsewhere and refuses an existing file.
- **Family k = 0** (correction 2026-11-26): a gateway family of a service has
  k = 0 in a cell where it was measured absent — a valid cell (no invalid
  repeat, two repeats, each with N > 0 answered as expected and a `gateway`
  read with sibling evidence: in a sampled cell some family of a
  `demo-<lang>-*` service counted above 0 in that repeat; in a `class:s00`
  cell, which counts 0 everywhere (unsampled inbound), some family series of
  the language present in the read, even at 0; the driver always writes
  `gateway`, `{}` included, so an empty read or one with no series of the
  language gives no k; a service
  whose own series are missing while a sibling's are present counts, as the
  copy's sibling rule, K-D12) whose repeats counted none of a family another cell of the service
  counted (`send` / `process` in the error scenarios, the rpc-client span for
  `unhandled`). An invalid or unmeasured cell gets no k (`missing`, so
  UNQUALIFIED). To apply it to a calibration that ran before the correction,
  without the cluster:

  ```bash
  R=testing/k8s/soak/runs/kcal-20261125d
  python3 -I testing/k8s/verify.py soak cal --run kcal-20261125d --out-dir "$R" \
    --input "$R/kcal-input.json" --rate 10 --output "$R/soak-cal.r5.json"
  ```

  (`--rate` = the run's `loaded_rate`, for the same `rate_cap`; exit 1 while a
  `rate_cap` is FAIL). `--output` is written atomically, mode 0600, and an
  existing file is refused before any read. `soak-cal.json` stays as
  written; pass `--kcal "$R/soak-cal.r5.json"` to `count` (r2: no
  sibling-evidence rule; r3: a count above 0 everywhere, which dropped the
  `s00` zeros; r4: a present series anywhere; r5 = the current rule, equal
  to r4 and r2 on this run).
- **Phase B.** One k6 run per step (default mix and classes), each followed
  by a quiesce. A step is clean when both windows are valid and the summary's
  `dropped_iterations` is 0; the steps stop at the first unclean one. Loaded
  rate = the highest clean step minus one step (none with fewer than two clean
  steps: exit 1). Drain per language from the gateway's `send` / `process
  demo:<lang>:orders`: a step saturated the worker when `send` − `process`
  (upper count) > max(10 msgs, 5 % of `send`); then its drain rate is the
  consumed count over the step's span (`measured`, the lowest such value); a
  worker that kept up has no measured rate (`unmeasured`, with the rate it
  reached). Then quiesces until the backlog is at most 10, at most the drain
  wait. `rate_cap` checks the loaded rate × the `ok` share against the
  measured drain rates; `k8b_rate` = min(loaded, floor(drain / `ok` share))
  per language and overall.
- **Estimate** (defaults, four languages): hook waits 10–21 min, phase A 74
  min, phase B 32 min, drain wait ≤ 15 min; 1.9–2.4 h (worst case 5.2 h).
  Exit 0 when both phases ran and a loaded rate exists; 2 when a `rate_cap`
  is FAIL (logged as `cal: FAIL rate cap ...`, run K8b at `k8b_rate`); 1
  otherwise.

## Shutdown, identity and labels (`shut`, task K-shut + K3 + K5)

SR15 "Shutdown (r6.2)", "Restart mechanics (r6.2)", "Identity (K3)",
"Labels (K5)"; SR7, SR12; design K-D22 (draft, 2026-11-26). Read-only
checks afterwards: `verify.py soak shut`, `soak identity`, `soak labels`.

- **Preconditions** (refused before any write): `run`'s pre-flight and entry
  gate; every api Deployment at replicas 1 (the lower bound assumes every
  pre request reached the one old process); `--kcal` a file; δ ≥ the pre
  scenario's graceful stop + 5 s (20 s), δ ≤ post − 60 s; pre and post ≥ 60
  s; deletion to t2 at most 25 min (the old instance's copy series expires
  after 30 min idle).
- **Pod watch.** Before the hook a child runs `kubectl get pods -l <demo
  selector> --watch --output-watch-events -o json` (its own session, ended
  with the other children; restarted when the API server ends it, each
  restart a recorded gap). The events are parsed in memory; only name, uid,
  phase, deletion time, start, `finishedAt`, exit code and reason are kept
  (`pods.jsonl`). The pod objects are never written: the hooked pods' env
  carries the canary header.
- **The nonce roll.** The hook's apply (its file carries a new
  `DEMO_RESTART_NONCE`) is the K3 roll: its old pods are the demo pods before
  the hook (on the shared agent), its new pods the hooked ones (through the
  copy). It is not a pure restart: it also moves the export from the shared
  agent to the copy. The promoted list is read from `prometheus/kps`
  `spec.otlp` (else the newest passing K1 snapshot, named).
- **The window.** As `run`'s loaded window, with a `tick` between guard
  reads: it reads the k6 container's `state.running.startedAt` (every 5 s
  until it has one); at `startedAt` + `--pre` + `--delta` (default 300 + 30
  s) it reads the copy's `otel_libs_soak_fixed_spans_total` per api over
  (t1, now] — the old `service_instance_id` is the one that grew (none or
  several: that api is UNQUALIFIED, named) — then deletes each api pod with
  `kubectl delete pods -l app.kubernetes.io/component=demo,app.kubernetes.io/name=demo-<lang>-api
  --field-selector metadata.name=<pod> --wait=false` (kubectl must print
  `pod "<pod>" deleted`). `deleted_at` is the time before the call; a guard
  read in progress delays it (`delete_delay_s`). Then the guarded quiesce and
  t2, as `run`.
- **Clocks and the post start.** `startedAt` is on the cluster clock: skew =
  the k6 Job's `creationTimestamp` − the apply's midpoint on the laptop
  (`clock` in `shut-input.json`); the planned delete is `startedAt` − skew +
  pre + δ. At the delete the tick reads `kubectl logs --timestamps` of the
  k6 Job: post start ≥ max(`startedAt` + pre, the first `phase=post`
  reservoir line − timeout, the first `phase=pre` line − timeout + pre), used
  for the post-request bound (`phase`). Reasons (the window is invalid):
  |skew| > δ − graceful stop − 5 s, or unmeasured; no post line before the
  delete, or the first post line after it (k6 init longer than δ); a delete
  more than one loop interval late, or more than post − 60 s after the post
  start. `deleted_at` in `shut-input.json` is on the cluster clock. The
  `--timestamps` prefix is the node's CRI time with its own UTC offset (on
  k8s01 `-03:00`, not `Z`; run `kshut-20261126`), parsed with any offset.
- **The deleted pods' logs.** Before the deletes, per api pod: one
  synchronous `kubectl logs --timestamps` snapshot, then a child `kubectl logs
  <pod> -c api --timestamps -f --since-time=<hook − 60 s>`, bounded at grace
  + 15 s; the deletes wait until each follower has emitted its first line, at
  most 2 s. The saved log is the snapshot plus the follow's lines after the
  snapshot's last timestamp; when the follow ends, one non-follow read if the
  pod is still there (a line-count cross-check). A failed follow keeps the
  snapshot (a finding); both failing make the source UNMEASURED. Saved as trap step 1b saves the demo logs (a canary line →
  the marker, 0600, counts only) in `raw/<pod>.api.deleted.log`, recorded in
  `shut-window.json` `deleted_logs`; `verify.py soak extras` scans each as its
  own source (a failed save is UNMEASURED).
- **The one guard exemption** (shut only; `run` and `cal` never set it): a
  guard (b) breach on the `demo->soak-agent` edge becomes a finding only when
  the guard's own verdict for that edge, re-read over the guard's own range
  (`guard.window_lookback`, 2 × the interval), is NOISY (UNKNOWN and ABSENT
  stay breaches), every increment of `istio_tcp_connections_failed_total` in
  that range comes from `demo-<lang>-api` of a deleted language, and the
  exempted steps equal the guard's counted increase. Windows: [deleted_at,
  finishedAt + 30 s] for non-`CONNECT` `response_flags` (the dying pod's
  connections), the replacement pod's first 30 s + 30 s for `CONNECT` ones; at
  most 3 increments over the window. Each exemption is recorded with its
  flags (`edge_exemptions`, `edge_flags_seen`) and attached to its api in
  `shut-input.json`: an api whose count is report-only (Go) and that had a
  deletion-window exemption is UNQUALIFIED in `verify.py soak shut` ("final
  flush unverifiable"). Every other edge stays strict.
- **The guard (c) exemption** (shut only, run `kshut-20261126`): the
  deletion makes k6's next connections to the dying pod refused at ztunnel
  (`connection failed: Connection refused` access lines, `dst.addr <ip>:15008`).
  A guard (c) breach becomes a finding only when the ztunnel logs, re-read
  from the guard's own read time (`--since` = now − at + the guard's range +
  10 s), hold, at or after at − the range, only lines with `src.workload` a
  pod of this window's k6 Job and `dst.workload` a deleted api pod, timed in
  [deleted_at, finishedAt + one guard interval] (cluster clock), exactly as
  many as the guard counted, at most 20 over the window; each line recorded
  sanitized (time, namespaces, workloads: `ztunnel_exemptions`). Non-JSON and
  every other line stay strict.
- **The k6 log** (all modes): `kubectl logs` of a suspended Job's deleted pod
  can exit 0 with "unable to retrieve container logs for containerd://..."
  (run `kshut-20261126`). A read without k6's banner, a reservoir line or the
  summary is not a k6 log: it goes to `<job>.log.err`, and `<job>.log` keeps
  the evidence — the stop-time save `<job>.stop.log` (copied), else an
  earlier usable `<job>.log`. The trap records the source per Job
  (`k6_log_source` in `trap.jsonl`), `shut` in `shut-window.json`.
- **Outputs.** `shut-input.json` (`verify.py soak shut --input`): t1, t2,
  `raw/soak-k6-shut.log`, the K-cal path (both relative to the file), per api
  `old_instance`, `pod`, `deleted_at`, `finished_at`, `exit_code`, `grace_s`
  (the pod's; `action_shut` uses it before a live Deployment read),
  `post_before_finish` (ceil(R × (`finishedAt` + 1 s − (`startedAt` + pre)))
  + 1, at most the post requests: the post requests issued before
  `finishedAt`, an upper bound), `new_pods`, `findings` (an exit code other
  than 0 or 137), and `unqualified` with a reason when the old instance or
  the deletion is unknown; `valid` and `reasons` of the window (`verify.py
  soak shut` refuses `valid: false`). `identity-input.json` (`verify.py soak identity`
  and `soak labels`): the hook time, t1, t2, the export interval (10 s), the
  promoted list, the rolls `nonce` (judged) and `shut` (report-only) with
  each old and new pod and the old pods' `finishedAt`. `shut-window.json`:
  t1, t2, `valid`, `reasons`, the deletions and the three verify commands.
  `workers.json` as `run`.
- **K3** (`verify.py soak identity`, `testing/k8s/soakident.py`): per roll
  and service, from Prometheus `/api/v1/series` of `job="dev-otel-libs/<svc>"`
  (app series carry `instance` and `k8s_pod_name`), each new pod has one
  instance, not an old one and not `unset`; it is the copy's
  `service_instance_id` (= `exported_instance`) and, when the service logged
  in (hook, t2], a Loki `service_instance_id`; each old instance's last
  sample is ≤ `finishedAt` + 10 s + 1 s: a raw range read
  `{job, instance}[900s]` at `finishedAt` + 10 min (each value keeps its own
  time), else, for an instance quiet by then, `max_over_time(timestamp(
  target_info{job, instance})[R:S])` (R from the pod's start, 15 min to 24 h,
  S = ceil(R / 2000) ≤ 240 s). `timestamp()` over all of an instance's metrics
  fails in Prometheus ("vector cannot contain metrics with the same labelset"
  once `__name__` is dropped; HTTP 422, run `kshut-20261126`). Any read error
  is an `ERROR` line and exit 1. Report-only: the final flush (last
  sample ≥ the delete request, on the cluster clock: K-shut's `deleted_at` +
  skew, else the watch's first `deletionTimestamp` − its
  `deletionGracePeriodSeconds`; the latest `deletionTimestamp` is the
  kubelet's final grace-0 delete, about finishedAt + 2 s), Loki ids of no recorded pod, and one Tempo
  search per new instance through `soakextras.TempoGuard`
  (`--no-identity-tempo` skips it). Exit 0 when every judged (nonce-roll)
  service is PASS. `verify.py soak shut` refuses `valid: false` with exit 2.
- **K5** (`verify.py soak labels`): per service and instance, on the app
  series: `job` = `service_namespace/service_name` = `dev-otel-libs/<svc>`,
  `instance` = `target_info`'s `service_instance_id`, every promoted
  attribute present on `target_info` on every series with the same value, no
  other resource label on a data series (the metrics that carry one are
  named); the series count per metric before
  (old instances) and after (new instances) of each roll. A restart or a
  rebuild makes new series (the `instance` label changes). The gateway's
  span-metrics series of the same `job` carry no `instance` and are counted
  apart (`other_series`), not judged.
- **Language scope.** The K-shut count is REPORT-ONLY for an api whose copy
  total K-cal did not qualify (Go in `kcal-20261125d`); exit within grace, K3
  and K5 are judged in every language.
- **Estimate** (defaults, four languages, `unmeasured`): pre-flight and entry
  gate 1–2 min, hook and its waits 5–21 min, k6 window 11–16 min (pull,
  600 s, graceful stop, summary, up to one read), quiesce 2 min, trap with the
  demo restore 3–6 min: about 25–45 min.

## The trap (EXIT, INT, TERM, HUP)

In order; each step runs whatever the earlier ones did (any exception is
recorded and the trap moves on), further signals are ignored while it runs,
and `runs/<id>/trap.jsonl` is written in any case:

1. save `kubectl logs job/<k6>` of every k6 Job of the run to
   `runs/<id>/raw/<job>.log`, with its size in bytes; a failed read goes to
   `<job>.log.err` and never overwrites a saved log; a log without a
   `SOAKRES` line, when the plan had a reservoir, is an error;
1b. only when the hook was applied: `kubectl logs --container=<c>
   --since-time=<hook time>` (stdout and stderr) of every demo container to
   `runs/<id>/raw/<pod>.<container>.log`, before the restore replaces the pods
   (`soak extras` scans them for the canary); `trap.jsonl` `demo_logs` records
   per container the lines, bytes and the number of canary lines (a count,
   never a line). A log with a canary line is never written: the trap saves a
   marker with the count instead; every saved log is mode 0600 and error
   texts are redacted;
2. delete only the objects labelled `otel-libs/soak-run=<id>` in
   `dev-otel-libs` (Jobs, ConfigMaps, Services, Pods, NetworkPolicies — by that
   selector, never by kind alone); the demo apps, the backing services and the
   GitOps copy `soak-agent` carry no such label;
3. re-read the namespace label `istio.io/dataplane-mode` and write `ambient`
   with `--overwrite` only if it is not `ambient` (a guarded no-op while K4 is
   deferred);
4. only when the hook was applied (`hook_applied` in `state.json`, set before
   the hook's `demo.sh apply`): `demo.sh apply --all` with `DEMO_RESTORE=1`,
   the recorded `TAG` and the recorded `LANGS` except those recorded at 0
   replicas, without `DEMO_ENV_FILE` and `RUN`; then `demo.sh scale --lang
   <lang> 0` (with `DEMO_RESTORE=1`) for a language recorded at 0, and
   `demo.sh scale --lang <lang> <n>` without it (demo.sh's normal capacity
   pre-flight) for one recorded above 1. `DEMO_RESTORE=1` skips the capacity
   pre-flight and refuses a scale-up, another running tag and a target at 0
   replicas: a restore only removes env from apps that already run.

## Soak-run objects (`render.py`)

Every object and pod template carries only `otel-libs/soak-run=<id>` and
`otel-libs/soak-role=<role>`, in `dev-otel-libs`. `Kube.apply` accepts only
Jobs, ConfigMaps, Services and Pods of `dev-otel-libs` labelled with the run's
own id. A run id is a label value (`soakplatform.RUN_RE`, shared with K1).

| Object | Notes |
|---|---|
| `soak-k6-<window>` ConfigMap + Job | `docker.io/grafana/k6:2.3.0@sha256:2dd4756e…` (multi-arch index, pushed 2026-10-24; 2.4.0 was under two weeks old), `backoffLimit: 0`, no TTL, `activeDeadlineSeconds` from the plan, memory limit 256 MiB + 4 MiB per concurrent VU (initial; K-cal replaces it with the measured peak) |
| `soak-otlp` Service | `copy`: ExternalName to `soak-agent`; `refused`: a selector-less ClusterIP; `hung` / `redirect`: ExternalName to the sink Service. ExternalName ↔ ClusterIP is a delete and a create. |
| `soak-fakeotlp-hung` / `-redirect` Pod + Service | `harbor.staffops.space/development/otel-libs-fakeotlp:ab24d24@sha256:08ac9e9f…` (pushed by K1); `HANG=true` for *hung*; *redirect* uses `/parity-redirect-c/` (307, token `parity-redirect-token`) |
| `soak-control` Job | telemetrygen v0.165.0 by digest, four containers, one trace each (`--traces 1 --child-spans 0`). Measured on k8s01 2026-11-23 (run `kcopy-20261123a`, Tempo): a trace is two spans, the CLIENT root `lets-go` and one SERVER child, both with the telemetry attributes; the SERVER span is always a fixed span. Containers: `rpc.system=grpc` (`<run>-ctl-a`), `rpc.system.name=grpc` (`<run>-ctl-b`), no rpc key (`<run>-ctl-n`, the negative case: SERVER only), and `rpc.system=grpc` without `service.instance.id` (`unset`). Expected on the copy (`render.control_expected`): `otel_libs_soak_fixed_spans_total` +2 for ctl-a, ctl-b and `unset`, +1 for ctl-n; `otel_libs_soak_spans_total{service_name="soak-control"}` +8. |
| `soak-fallback-<lang>` Service | SR6: port 9464 of `demo-<lang>-api`, read through the service proxy |

## k6 script (`k6script.py`)

- `POST /orders` on `http://demo-<lang>-api:8080`, open loop
  (`constant-arrival-rate`), one scenario per language (and per phase,
  `phase=pre|post`, for K-shut).
- Fixed mixes by iteration number: scenarios `ok` 7, `business_error`,
  `db_error`, `unhandled` 1 each (the demo worker's cycle); inbound classes
  `none`, `s01`, `s00` (a random W3C `traceparent`, sampled or not).
- One no-op threshold (`count>=0`) per cell (language × route × scenario ×
  class [× phase]) and outcome (`expected` = the scenario's status 201 / 422 /
  409 / 500, `unexpected`, `noanswer` = status 0) on the counter
  `soak_requests`. The business scenario is tagged `scen`: `scenario` is a k6
  system tag.
- `maxVUs = ceil(rate × (timeout + p99))`, request timeout ≤ 15 s (default
  10 s, p99 1 s until K-cal); `gracefulStop = timeout + 5 s`;
  `activeDeadlineSeconds = pull (300 s) + duration + gracefulStop + summary (60 s)`.
- `handleSummary` prints the summary JSON between `SOAK_SUMMARY_BEGIN` and
  `SOAK_SUMMARY_END`.
- **Orphan reservoir: the fallback.** `handleSummary` gets only aggregated
  metrics and VUs share no mutable state, so the summary cannot carry
  per-request ids on k6 2.3.0. The script prints one `SOAKRES {json}` line
  (`trace_id`, `parent_id`, class, status, VU) for 50 rounds of each
  language scenario, a round being `len(mix) × len(classes)` consecutive
  iterations (`exec.scenario.iterationInTest`), one round every `stride` =
  max(1, floor(planned rounds / 50)) (19 for 4 req/s × 2 h), so the logged
  rounds spread over the window and every cell of every language
  logs (50 per cell, 350 for `ok` with the 7/1/1/1 mix) and a 4-language plan
  logs at most 50 × 30 × 4 lines whatever `maxVUs` is (`--log-format=raw`);
  the former gate on VUs 1 and 2 (`idInTest`, global across scenarios) left Go
  and Node.js with no line in `k8b-20261125`. The trap's `kubectl logs`
  (without `--timestamps`, which would break the markers) saves them.
  `fixtures/k6-2.3.0-run.log` is a recorded run of the pinned image: every
  per-cell sub-metric came back in the summary as written, every language
  logs reservoir lines within the round bound (re-recorded 2026-11-26 with
  the round gate), and `dropped_iterations` is absent
  when it is 0.

## Evidence

`runs/<id>/` is git-ignored: `state.json` (tag, languages, the HEAD `commit` and `dirty` flag, the running demo `digests` at the window start — since 2026-11-27), `guard.jsonl`
(every read), `windows.jsonl`, `trap.jsonl`, `heartbeat`, `raw/` (k6 logs and, with
`--hook`, the demo container logs of trap step 1b),
`count-window.json` and, with `--hook`, `workers.json` (`run`, above),
`shut-input.json`, `identity-input.json`, `shut-window.json` and `pods.jsonl`
(`shut`, above),
and, with `--hook`, `soak.env` and `canary` (mode 0600, also when the file
existed; the canary is never printed; K11 scans the summary for it). Every
other file the loop writes goes through `verify.scan_secret` first: a flagged
text is replaced by a marker naming the patterns.

## Tests

```bash
python3 -I -m unittest discover -s testing/k8s/soak -p 'test_*.py'
python3 -I testing/k8s/demo/test_soakextras.py   # verify.py soak extras
python3 -I testing/k8s/demo/test_soakident.py    # verify.py soak identity and labels
```

No cluster: the tests run against `fakekube.py` and K1's fake Prometheus
reader (`testing/k8s/demo/test_platform.py`, which honors range bounds).
