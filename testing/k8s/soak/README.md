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

## The trap (EXIT, INT, TERM, HUP)

In order; each step runs whatever the earlier ones did (any exception is
recorded and the trap moves on), further signals are ignored while it runs,
and `runs/<id>/trap.jsonl` is written in any case:

1. save `kubectl logs job/<k6>` of every k6 Job of the run to
   `runs/<id>/raw/<job>.log`, with its size in bytes; a failed read goes to
   `<job>.log.err` and never overwrites a saved log; a log without a
   `SOAKRES` line, when the plan had a reservoir, is an error;
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
  (`trace_id`, `parent_id`, class, status, VU) for the first 50 requests per
  cell per VU, from VUs 1 and 2 only (`reservoir_vus`), so a cell logs at most
  100 lines whatever `maxVUs` is (`--log-format=raw`); the trap's `kubectl
  logs` (without `--timestamps`, which would break the markers) saves them.
  `fixtures/k6-2.3.0-run.log` is a recorded run of the pinned image: every
  per-cell sub-metric came back in the summary as written, the reservoir is
  bounded per cell per VU and across VUs, and `dropped_iterations` is absent
  when it is 0.

## Evidence

`runs/<id>/` is git-ignored: `state.json` (tag, languages), `guard.jsonl`
(every read), `windows.jsonl`, `trap.jsonl`, `heartbeat`, `raw/` (k6 logs),
`count-window.json` and, with `--hook`, `workers.json` (`run`, above),
and, with `--hook`, `soak.env` and `canary` (mode 0600, also when the file
existed; the canary is never printed; K11 scans the summary for it). Every
other file the loop writes goes through `verify.scan_secret` first: a flagged
text is replaced by a marker naming the patterns.

## Tests

```bash
python3 -I -m unittest discover -s testing/k8s/soak -p 'test_*.py'
```

No cluster: the tests run against `fakekube.py` and K1's fake Prometheus
reader (`testing/k8s/demo/test_platform.py`, which honors range bounds).
