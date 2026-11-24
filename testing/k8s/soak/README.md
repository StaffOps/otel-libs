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
| `soak.py` | CLI, the window loop, the trap, `cleanup`, the pre-flight, the dry run |
| `guard.py` | the guard read: a thin adapter over K1's `testing/k8s/soakplatform.py` (guard (a)–(h), sentinels, edges, `sel`) |
| `render.py` | every soak-run object, as JSON |
| `k6script.py` | the k6 script generator and the summary / reservoir parser |
| `fakekube.py` | a fake `kubectl` (and `demo.sh`) for the tests and the dry run |
| `test_*.py`, `fixtures/` | unit tests; a recorded run of the pinned k6 |

## Modes

| Mode | Does |
|---|---|
| `preflight --run <id> [--tag <t>]` | Reads only. Refuses while any object labelled `otel-libs/soak-run` exists (of any run id), an object without that label carries a soak name (`soak-otlp`, `soak-k6-*`, …), or the namespace label `istio.io/dataplane-mode` is not `ambient`; records the running demo (one image tag, the replicas per language, from the live Deployments) and refuses when the tag is not `--tag` or `demo-tag.sh`'s; then the entry gate, one line per item. |
| `run --run <id> --window <name> --duration <s> [--rate <n>] [--langs ...] [--hook] [--control] [--worker-interval <s>] [--tag <t>]` | The pre-flight and the entry gate first, before any write. Then `soak-otlp` (copy variant); with `--control` (task K-copy), the positive control before the hook: the copy's `soak-control` series read, the `soak-control` Job applied and waited for (360 s), the counts polled until they arrive (120 s) and read again two scrapes (30 s) later; PASS only when the delta is exactly `render.control_expected` (`otel_libs_soak_spans_total` +8; `otel_libs_soak_fixed_spans_total` +2 for `<run>-ctl-a`, `<run>-ctl-b` and `unset`, +1 for `<run>-ctl-n`; an absent series counts 0, a decrease is a copy reset); `control.json`, a `windows.jsonl` record and `raw/soak-control.log`; a FAIL ends the run before the hook (the trap runs); with `--hook`, the demo re-applied through `demo.sh`'s `DEMO_ENV_FILE`, guard (g) polled until the rolled pods are Ready for 10 min (Terminating pods skipped; at most 10 + 10 min), and guard (h) as an increase of every service's copy count since the hook; with `--rate`, the k6 ConfigMap and Job; then a guard read every 60 s until the window ends. The trap always runs at the end. |
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
  (an item of (a)–(h)) or `platform: guard (<item>)`; `kubectl logs job/<k6>`
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
