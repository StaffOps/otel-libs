# Soak summary — Phase K on `k8s01`, 2026-11-23 … 2026-11-26 (task K11)

Evidence for `0.3.0` final: config-contract
[D10 — Release](../../config-contract-v0.3/design.md#d10--release) links this
page. Spec: [requirements](../requirements.md) ("Acceptance in Phase K terms
(r6.2)"), [design](../design.md) (K-D10…K-D22), [tasks](../tasks.md) (r6.2).
Assembled 2026-11-26, updated 2026-11-27 (owner rulings and K11 review
round 1); round 2 of the refutation and `code-review` pending.

Per-run reports (written by `verify.py soak report`, secret scan inside):

| Run | Task | Window (UTC) | Soaked commit (tag) | Report |
|---|---|---|---|---|
| `k8a-20261124b` | K8a passive | 2026-11-24T15:16:59Z … 2026-11-25T03:16:59Z (12 h) | `1404f9c` (`soak/k8a-20261124b`) | [k8a-20261124b.md](k8a-20261124b.md) |
| `kcal-20261125d` | K-cal | 2026-11-25T19:16Z … 21:41Z | `1d683b2`, inferred from commit times (newest commit before the run start) — **no tag** | [kcal-20261125d.md](kcal-20261125d.md) |
| `k8b-20261125` | K8b + K8b-x | 2026-11-25T22:22:32Z … 2026-11-26T00:22:36Z (k6 2 h) | `e5f8e4b` (`soak/k8b-20261125`) | [k8b-20261125.md](k8b-20261125.md) |
| `kshut-20261126c` | K-shut + K3 + K5 | 2026-11-26T20:40:02Z … 21:18:54Z | `581e9b8` (`soak/kshut-20261126c`) | [kshut-20261126c.md](kshut-20261126c.md) |

Each per-run report's "Commit" is the commit of its tag (`git rev-parse
refs/tags/soak/<run>^{commit}`; K-cal: not recorded), HEAD only as "generated
at" (`+dirty` while `testing/k8s` has uncommitted changes); its image digests
are read from the live demo Deployments, whose tag equals the run's. They
were not recorded at run time; the Harbor check under "Code freeze" proves
they are the digests that ran (tags pushed before K8a, no re-push). Demo image tag in every run: `7080d19` (`state.json`), equal to
`demo-tag.sh` at every tag and at HEAD. Raw output is in
`testing/k8s/soak/runs/<run>/` (git-ignored).

Conditions of every result: `deployment.environment.name=dev` only (AG5),
ambient mesh without waypoint or mesh tracing (AG10), plaintext agent-only
Collector copy `soak-agent` (AG1, AG2), the homelab Prometheus (AG4).

## Verdicts per language and check

Legend: PASS; FAIL; REPORT-ONLY (measured, no verdict by rule or ruling);
UNMEASURED (no data for the verdict); UNQUALIFIED (the tool's verdict: the
measurement exists, a precondition for judging it failed).

| Check (run) | Go | Python | Node.js | .NET |
|---|---|---|---|---|
| K8a passive, S-D5 rules, 3 containers (`k8a-20261124b`) | PASS (AG20) | PASS (AG20) | PASS (runtime n/a, AG12; AG20) | PASS (AG20) |
| K-cal fixed-family k, 3 services (`kcal-20261125d`) | FAIL (excluded, named; AG17) | PASS | PASS | PASS |
| K8b copy fixed families, 3 services (`k8b-20261125`) | UNQUALIFIED (tool); report-only by ruling (AG17) | PASS (exact) | PASS (exact) | PASS (exact) |
| K8b gateway cross-check, recorded t2 = k6 end + 120 s | UNQUALIFIED | DUPLICATES / UNQUALIFIED ¹ | DUPLICATES / UNQUALIFIED ¹ | DUPLICATES / UNQUALIFIED ¹ |
| K8b gateway cross-check, t2 = k6 end + 16 s | UNQUALIFIED | counts exact ² | counts exact ² | counts exact ² |
| K8b drain (send = process) | UNQUALIFIED | PASS | PASS | PASS |
| K8b api → backend failed share | REPORT-ONLY | PASS | PASS | PASS |
| K8b secret canary (25 sources) | UNMEASURED ³ | UNMEASURED ³ | UNMEASURED ³ | UNMEASURED ³ |
| K8b SR9 orphans (0 orphans in 276 traces) | UNMEASURED | UNQUALIFIED | UNMEASURED | UNQUALIFIED |
| K8b SR10 parity (cross-language) ⁴ | FAIL: 44 waiting for rows, 7 AG18 / AG19; AG16 measured gap | FAIL: 51 waiting, 2 AG14, 7 AG18 / AG19; AG16 UNMEASURED | FAIL: 54 waiting, 6 AG18 / AG19; AG16 UNMEASURED | FAIL: 34 waiting, 6 AG15, 8 AG18 / AG19 |
| K8b per-severity log counts (AG6) | REPORT-ONLY | REPORT-ONLY | REPORT-ONLY | REPORT-ONLY |
| K-shut old-process count (`kshut-20261126c`) | REPORT-ONLY | PASS | PASS | PASS |
| K-shut exit within grace (15 s), code 0 | PASS (−0.04 s ⁵) | PASS (0.84 s) | PASS (3.74 s) | PASS (0.61 s) |
| K-shut canary, demo and deleted-pod logs | PASS (0 lines scanned; the apps write no stdout/stderr) | PASS (0 lines scanned; the apps write no stdout/stderr) | PASS (0 lines scanned; the apps write no stdout/stderr) | PASS (0 lines scanned; the apps write no stdout/stderr) |
| K3 identity, nonce roll, 3 services | PASS | PASS | PASS | PASS |
| K3 identity, shut roll (api) | REPORT-ONLY (PASS) | REPORT-ONLY (PASS) | REPORT-ONLY (PASS) | REPORT-ONLY (PASS) |
| K5 labels, 3 services | PASS | PASS | PASS | PASS |
| K10 / K10b failure modes | not run (AG13) | not run (AG13) | not run (AG13) | not run (AG13) |

1. DUPLICATES +14 on `POST /orders` and `demo.order.accept`, +10 to +14 on
   the send / process and RPC families, identical in the three languages:
   the trap restored the demo 9 s after k6 ended, so the restored pods'
   normal traffic reached the gateway inside (t1, t2]. The copy was not
   affected (the restored pods do not export to it). Fixed in `1ac972b`.
   Source: `runs/k8b-20261125/soak-count.json` (recorded with
   `kcal-20261125d/soak-cal.json`, before the k = 0 fix).
2. Re-derived 2026-11-26 with `verify.py soak count --output
   runs/k8b-20261125/soak-count.t2-k6end+16s.json` (read-only,
   `--kcal soak-cal.r5.json`): every gateway family equals its expected
   count (19 200, 17 280, 13 440; drain 13 440 = 13 440) in the three
   languages; the tool's verdicts are UNQUALIFIED only because the flat
   `workers.json` (built after the trap, defect 2 of K8b) makes the live
   pods the restart check, and those pods were replaced by later runs.
   `soak-extras.json` uses the same t2.
3. No hit in 25 sources (every Loki stream of both namespaces, the four
   Collector containers, 2 382 fetched span values, the k6 log, the output);
   UNMEASURED because the window's demo container logs were not saved (run
   before trap step 1b). `kshut-20261126c` saved them: 0 lines in the 12
   demo and 4 deleted-pod logs (the apps write nothing to stdout or stderr
   under OTLP). K8a and K-cal were not scanned. Two limits: `kubectl logs`
   returns only the container's current log file, so a file the kubelet had
   rotated is not scanned; and the SDK error-text path under export failure
   (where a header could leak) was not exercised — the failure modes are
   K10, deferred as AG13.
4. SR10 is one verdict over the four languages: 354 items, 174 documented,
   173 FAIL, 7 UNMEASURED, judged against the allowlist before the owner
   rulings. Re-judged 2026-11-27 offline with `verify.py soak extras --run
   k8b-20261125 --rejudge runs/k8b-20261125/soak-extras.json --output
   runs/k8b-20261125/soak-extras.rejudge-20261127.json` against the current
   `testing/k8s/soak/sr10-gaps.json`: **254 documented, 16 UNMEASURED,
   84 FAIL**.
   - Documented: 174 runtime families plus 80 items matched by `accepted`
     entries (75 of 2026-11-26, the 2 Redis `connect` spans, the 4 demo
     start-up commands; the item and entry counts differ by one).
   - UNMEASURED: the 7 recorded by the traffic rule, incl. the AG16 item;
     and 9 worker `traces_span_metrics_*` label keys (`error_type`,
     `http_request_method`, `http_response_status_code` × calls / duration
     count / sum, missing in Python, Node.js, .NET), UNMEASURED by owner
     ruling 2026-11-26: these keys belong to HTTP CLIENT spans that only the
     Go worker emitted. They are `unmeasured` entries in the allowlist,
     scoped to this run (`runs: ["k8b-20261125"]`), which the tool honours
     only for that run id.
   - FAIL, 84 items: 27 items matched by `proposed` entries (31 entries)
     and 39 by `needs-row` entries (23 metric + 16 span), waiting for their
     rows (A50); 18 accepted gaps (AG14 Python DB duration metric 2, AG15
     .NET DB duration attributes 6, AG18 SQL query span names 8, AG19 Go
     HTTP client span name 2). Per-language counts in the table are by
     missing language.
   - AG16 (Go `http.client.request.duration` without `error.type`) is a
     measured gap in Go, confirmed from source: otelhttp v0.72.0
     `internal/semconv/client.go:175-178` puts `error.type` on the spans,
     `:240-264` / `:281-284` build the metric without it; 434 failed of
     1 448 Go worker → api calls. Python and Node.js are UNMEASURED (no
     traffic for the metric).
5. `finishedAt` has whole-second resolution, so an exit time is ±1 s; the
   check accepts −1 s < took (`4c56659`).
6. Report-only parity observation: the api ERROR record counts differ by
   language in `k8b-20261125` (Python 8 637, Node.js and .NET 5 758, Go
   3 023; `severity_number` 17), from the demo's per-language logging.

SR10 span name / kind differences (32, `k8b-20261125`), classified
2026-11-27 by the observability refuter and **ruled by the owner the same
day** (last column):

| Group | Count | Items (role: span name — languages that have it) | Ruling 2026-11-27 |
|---|---|---|---|
| Documented already: Redis `connect` (gaps row "Redis db.operation.name and span names") | 2 | api: `connect` — go, node; worker: `connect` — go, node | `accepted` (owner 2026-11-27: accepted, gaps row "Redis db.operation.name and span names") |
| Can be documented — Go `CLIENT` (`CLIENT SETINFO`) | 2 | api, worker: `CLIENT` — go | `needs-row` (A50 row) |
| Can be documented — `SET` / `SETEX` | 2 | api: `SET` — go, python, node; `SETEX` — dotnet | `needs-row` (A50 row) |
| Can be documented — `XINFO` subcommand naming | 2 | worker: `XINFO` — go, node; `XINFO GROUPS` — python | `needs-row` (A50 row) |
| Can be documented — Python ASGI `http receive` / `http send` INTERNAL spans | 2 | api: `POST /orders http receive`, `POST /orders http send` — python | `needs-row` (A50) + task A59 (`exclude_spans`) |
| Can be documented — Node.js express `request handler` | 1 | api: `request handler - /orders` — node | `needs-row` (A50 row) |
| Can be documented — SQL connection spans per language | 5 | backend: `CONNECT demo_dotnet` — dotnet; `connect` — python; `pg-pool.connect`, `pg.connect` — node; `sql.connector.connect` — go | `needs-row` (A50 row) |
| Can be documented — Go XSAM `sql.rows` / `sql.conn.reset_session` | 2 | backend: `sql.rows`, `sql.conn.reset_session` — go | `needs-row` (A50 row) |
| Real gap, fix after the freeze — SQL query span names (none uses `db.query.summary` / `db.collection.name`; Python `INSERT demo_python` has the `{op} {target}` form with `db.namespace` as the target; .NET `postgresql` is the last-resort fallback; Node.js `pg.query:-- demo_node` comes from the schema's leading SQL comment) | 8 | backend: `INSERT demo_python`, `SELECT demo_python` — python; `pg.query:-- demo_node`, `pg.query:INSERT demo_node`, `pg.query:SELECT demo_node` — node; `postgresql` — dotnet; `sql.conn.exec`, `sql.conn.query` — go | AG18, fix A57 |
| Real gap, fix after the freeze — Go HTTP client span name (otelhttp's default formatter) | 2 | worker: `HTTP POST` — go; `POST` — python, node, dotnet | AG19, fix A58 |
| Tool artefact — demo start-up commands differ per language | 4 | api: `EXISTS`, `XINFO GROUPS` — python; worker: `EXISTS` — python, node; `TYPE` — go | `accepted`, "demo code, per language (DE-D7)" |

Row texts for `docs/site/library/overview.md` "Known emitted-telemetry gaps"
(A50 pass; the refuter's wording, as relayed): Go `CLIENT` SETINFO; `SET` /
`SETEX`; `XINFO` subcommand naming; Python ASGI `http receive` / `http send`
INTERNAL spans; Node.js express `request handler`; SQL connection spans per
language; Go XSAM `sql.rows` / `sql.conn.reset_session`.

## Acceptance (r6.2) item by item

| Item | Verdict | Evidence (run) |
|---|---|---|
| K1 snapshot: runtime-metric names, `exported_instance`, 1 h floor reading, Prometheus PVC | PASS (B1 met 2026-11-27) | `runs/k1-20261123/snapshot.json` (66/66 PASS). Re-run 2026-11-27T14:10Z (`runs/k1-20261127`): 65/65 PASS; the 24 h baseline counters were clean (0 noisy, 0 absent), but a roll-list container started at 2026-11-26T19:01:51Z, so a clean 24 h baseline is readable only from 2026-11-27T19:01:51Z. Re-run 2026-11-27T19:05:18Z (`runs/k1-20261127b`): 65/65 PASS; baseline [2026-11-26T19:05:19Z, 2026-11-27T19:05:19Z] with 0 noisy, 0 absent, 0 n/a, and no roll-list container start inside it (youngest: `tempo-0` at 2026-11-26T19:01:50Z) — **B1 met**. Every window's entry gate passed (`windows.jsonl` valid) |
| K-loop: dry run creates one soak-run object of each kind, the trap deletes exactly those; unit tests | PASS | `soak.py dry-run` PASS (9 objects, 29 seeded untouched), re-run 2026-11-26; the soak loop suite (276 tests) and the demo-side suite (439 tests, incl. `test_soakcount` 102, `test_soakextras` 73, `test_soakident` 19) pass on host `python3 -I` (2026-11-27) |
| K-copy: positive control N sent = counted = accepted; K2 shows the demo pods' `k8s.pod.name` | PASS | `runs/kcopy-20261123d/control.json` (+8 / +2 / +2 / +1 / +2); tasks K-copy note |
| K8a: coverage gate, valid passive window, S-D5 rules, FDs by `exec`, `verify.py demo` at start and end, mesh and platform guard clean | PASS with AG20 (12 h ruling, reduced sensitivity accepted 2026-11-25: a leak stepping less often than every 5 h 15 min can pass; 90 min warm-up vs the 12 h settling; node-worker at 0.89 of its limit, node-worker and dotnet-worker still stepping) | [k8a-20261124b.md](k8a-20261124b.md): 72 rules PASS, 3 n/a (AG12); 721 window reads, 723 guard reads, 0 non-ok, 1 transport retry; lowest hourly coverage 0.9667; `verify.py demo` after the window PASS 56, XFAIL 2, NA 6. The start-of-window `verify.py demo` is not recorded: accepted gap AG20 (owner 2026-11-27; guards and sentinels clean for the 12 h) |
| K-cal: k integral and stable over two repeats or excluded and named; drain and loaded rate recorded | PASS | [kcal-20261125d.md](kcal-20261125d.md): 9 services qualified, Go's three excluded and named; drain 3.029–3.14 msg/s; steps 2/5/10/20 req/s clean → loaded rate 10; rate cap FAIL at 10 (7 msg/s enqueued) → K8b at 4 req/s (owner); step 20 saturated (backlog ≈ 1 500 per language) and the drain wait ended undrained (`drained: false`, 988 s against the planned 900 s, ≈ 1 130 left) |
| K8b: no LOSS / DUPLICATES on fixed families, clean sentinels, drain, service-graph share, canary, SR9, SR10 | NOT MET as written; owner accepted K8b on the copy verdicts (2026-11-26) | [k8b-20261125.md](k8b-20261125.md): copy PASS exact (9 services), sentinels clean (copy and shared gates), drain PASS (3), graph PASS (3); canary UNMEASURED (the SDK error-text path under export failure not exercised: K10 deferred, AG13); SR9 UNQUALIFIED / UNMEASURED; SR10 FAIL (re-judge 254 / 16 / 84: 66 waiting for rows, 18 accepted gaps AG14 / AG15 / AG18 / AG19) |
| K-shut, K3, K5 pass | PASS (Go count report-only) | [kshut-20261126c.md](kshut-20261126c.md): count PASS .NET 3 228 in [2 884, 3 495], Node.js 3 227 in [2 884, 3 547], Python 3 216 in [2 880, 3 499]; exit .NET 0.61 s, Node.js 3.74 s, Python 0.84 s, Go −0.04 s of 15 s (`finishedAt` ±1 s); canary PASS (0 lines scanned; the apps write no stdout/stderr); K3 12/12 + 4/4; K5 12/12 |
| K10 (refused, hung, back-pressure, redirect) and K10b | Accepted gap AG13 (owner 2026-11-25, recorded in requirements and D10 2026-11-26) | tasks.md "Deferred in r6.2" |
| Every failure fixed and re-run, or an owner-ruled accepted gap (AG1–AG20, amended 2026-11-26 and 2026-11-27) | OPEN | K8a start reading: AG20; Go exclusion: AG17 (DE16); SR10 material gaps: AG14 (A54), AG15 (A55), AG16 (A56); K10: AG13. SR10 span names: AG18 (A57), AG19 (A58). Still open: SR10's 66 entries waiting for their rows (A50); K8b canary UNMEASURED; SR9 UNQUALIFIED / UNMEASURED |
| D10 (1) image digests recorded | PASS by Harbor proof | Not recorded at run time; Harbor (read 2026-11-27, anonymous GET) shows all twelve `otel-libs-demo-*:7080d19` tags pushed 2026-11-23T18:38–18:52Z, before K8a started (2026-11-24T14:26:14Z), at the digests the pods run — "Code freeze" below |
| Code freeze held since `soak/<run>`; `0.3.0` differs only in version strings, CHANGELOG, docs, and (corrected 2026-11-26) files that are neither a library nor a demo image input (`testing/`, `tools/`, `.github/`, `mkdocs.yml`, specs) | Freeze PASS; `0.3.0` diff not yet checkable | "Code freeze" below |
| Summary passed the secret scan, committed, refuted, linked from D10 and the CHANGELOG | Scan PASS; D10 link added 2026-11-26; commit, refutation and CHANGELOG link pending | "Secret scan" below |

## Owner rulings in this soak

- 2026-11-24: the soak does not read guard (a) (node-wide ztunnel count;
  other namespaces' completed jobs broke `k8a-20261124a`).
- 2026-11-24: K8a is 12 h = 90 min warm-up + 10 h 30 min judged (was 30 h,
  K0e-1); 2026-11-25: the PASS is accepted with the reduced sensitivity
  (S-D5, SR15 amended); reversal: a 30 h re-run.
- 2026-11-25, "Enxuto": K-cal runs only what K8b and K-shut consume (new
  task K-cal-d); K10 + K10b deferred to after `0.3.0` final as AG13.
- 2026-11-25: K8b at 4 req/s per language (floor(drain / 0.7)).
- 2026-11-26: K8b accepted on the copy verdicts.
- 2026-11-26: SR10 allowlist — the 75 `proposed` entries with an existing
  gaps row are `accepted`; 31 `proposed` without a row and 23 `needs-row`
  wait for their rows (A50 docs pass); the material gaps are documented in
  A50 and fixed after K11: A54 Python DB duration metric, A55 .NET DB
  duration attributes, A56 Go HTTP client `error.type`; SR10 of
  `k8b-20261125` stays FAIL with the cause recorded.
- 2026-11-26: the guard (f) question (`up == 1` during the K10
  back-pressure flip) is deferred with AG13.
- 2026-11-26: Go stays report-only in this soak; the Go demo interval cap is
  demo task DE16, after the freeze, then a Go K-cal and K8b.
- 2026-11-26: CI runs the soak loop tests (`a0b37b7`).
- 2026-11-26 (K11): AG14 = A54, AG15 = A55, AG16 = A56, AG17 = DE16
  accepted, each fixed after the freeze; AG13 and AG14–AG17 recorded in
  requirements "Acceptance (r6.2)", "Accepted gaps" and D10; D10 rule (2)
  corrected (tooling, CI, `mkdocs.yml` and specs may change); the A56
  item is UNMEASURED, not FAIL; the 9 worker label-key items are
  UNMEASURED; the 32 span differences go to the observability refuter.
- 2026-11-27: on the refuter's classification of the 32 span differences —
  AG18 (8 SQL query span names, fix A57) and AG19 (2 Go HTTP client span
  names, fix A58) accepted; 16 documentable items `needs-row` in
  `sr10-gaps.json` (incl. the Python ASGI pair, plus task A59); the 4 demo
  start-up commands `accepted` ("demo code, per language (DE-D7)").
- 2026-11-27: the 2 Redis `connect` span items confirmed `accepted`
  (gaps row "Redis db.operation.name and span names").
- 2026-11-27: the missing start-of-window `verify.py demo` of K8a is
  accepted gap AG20 (the end-of-window check passed; guards and sentinels
  clean for the 12 h).

## Platform findings

- Tempo blocklist poll failed from 2026-11-25T16:23Z on two zero-byte blocks
  (moved aside, not deleted), so only the live store (about 20 min) was
  queryable; fixed by the platform session 2026-11-26.
- `tempo-0` OOMKilled at 2026-11-26T15:53:35Z by unthrottled trace-by-ID
  reads of the first `extras` run; the platform raised Tempo's memory limit
  to 4 GiB (platform commit `085be8f`, owner approval). The tool now reads
  Tempo sequentially at ≤ 10 reads/min with a working-set stop (3 GiB
  default, `98dfd8e`); the K8b-x run peaked at 0.659 GiB.
- Go demo worker: `demokit.EnvSeconds` returns the 5 s default for any value
  above 3600 (`go/demo/internal/demokit/demokit.go:128`), so Go drove every
  5 s in K-cal (21 600 s requested) and K8b (10 800 s). Measured in K8b:
  `k8b-20261125/soak-extras.json` `.graph.go.worker_api_growth.total` =
  1 448 worker → api calls over 7 249 s, one per 5.006 s. Inferred for
  K-cal: in all 12 Go cells (both repeats) the copy delta beyond k × N is
  constant (api 129–142, backend 64–72, worker 25–28 spans per ≈ 188 s
  repeat), independent of scenario and class; one drive per 5 s is ≈ 37
  drives per repeat, of which the 7/10 `ok` mix gives ≈ 26 `ok` orders. Fix
  is DE16 (AG17).

## Watch list (K8a, `k8a-20261124b`)

| Container | Signal | Measured |
|---|---|---|
| node-worker | RSS stepped until about 7 h, flat at 58.4 MB from 22:16Z | full-span slope 513 994 B/h = 0.89 of its limit 579 502; second half 53 892 B/h |
| dotnet-worker | RSS stepped until about 8 h | full-span slope 97 478 B/h of limit 509 829; second half −799 387 B/h |
| dotnet-api | thread-pool threads 4 ↔ 5 noise | slope 0.0097/h, second half 0.023/h, limit 0.19/h |
| python-backend | threads, one step | slope 0.124/h of limit 0.19/h, second half 0 |

Observability scoring of K8a: Go 9.5, Python 9, Node.js 6.5, .NET 8.
Reversal (S-D5): any container failing or ending above 0.9 of a limit in a
later window, or a release needing the SR4 leak claim → K8a at 30 h.

## Tooling defects found during the soak

| Found in | Defect | Fix |
|---|---|---|
| `k8a-20261123a` | one API-server `connection reset by peer` made a guard read blind and ended the window | `592526e` (transport retries, `retries` / `retry_error` in `guard.jsonl`) |
| `k8a-20261124a` | guard (a) mismatched on completed jobs of other namespaces | `f2b34b6` (guard (a) not read, owner ruling) |
| `k8a-20261124b` verdict | `--judged 10h30m` rejected; only `630m` parses (`verify._duration`) | not fixed; documented workaround |
| `k8a-20261124b` verdict | coverage written only on a failure; the hourly bins missed the last partial hour | `11088cc` |
| K-cal attempts | guard (c) counted monitoring scrape refusals outside the run path | `17ba1a8`, `6521270` |
| K-cal attempts | demo PostgreSQL init check refused `demo.sh apply` after node reboots | `c3e2bb9` |
| `kcal-20261125b` | a k6 container that exited 0 at the pull allowance invalidated the window (phase B lost) | `1d683b2` |
| `k8b-20261125` | trap restored the demo before the quiesce (gateway DUPLICATES); `workers.json` built after the trap; no k = 0 for absent families | `1ac972b` |
| `k8b-20261125` (K8b-x) | the k6 reservoir gate was global, so Go and Node.js logged no record; demo container logs not saved by the trap | `cc9cdf9` (per-scenario reservoir, trap step 1b) |
| K8b-x first run | unthrottled Tempo reads OOMKilled `tempo-0` | `cc9cdf9` (rate and memory guard), `98dfd8e` (3 GiB stop) |
| K8b-x | allowlist test did not accept owner-accepted entries | `fd74225` |
| `kshut-20261126` | guard (c) at the deletion, k6 timestamp offset, log save overwrite, K3 query HTTP 422 | `64aa719` |
| `kshut-20261126b` | guard (h) summed copy series; a hook worker interval above Go's 3600 s cap accepted | `581e9b8` |
| `kshut-20261126c` | exit check ignored `finishedAt`'s whole-second resolution | `4c56659` |
| K11 (this page) | `verify.py soak report` wrote HEAD at generation as "Commit", not the soaked commit; no image digests (no `snapshot.json` in the run directories) | uncommitted: Commit from the tag (`git rev-parse --verify`) or `state.json`, a missing tag named, HEAD as "generated at" (`+dirty`); digests from `snapshot.json`, else `state.json`, else the live demo Deployments only at the run's tag (pods by selector, container by name, same repository; kubectl errors degrade to "not recorded"), with the re-push caveat; `soak.py` records `commit`, `dirty` and the demo digests in `state.json`; case-insensitive canary / token scan; heuristic candidate rows labelled (`soakcount.py`, `soak/soak.py`; tests in `test_soakcount.py`, `soak/test_loop.py`) |
| K11 (this page) | SR10 could not reproduce owner-ruled UNMEASURED items, nor re-judge a saved run | uncommitted: allowlist status `unmeasured`, honoured by `parity_items`; `verify.py soak extras --rejudge` (offline; `soakextras.py`, tests in `test_soakextras.py`) |

## Code freeze (K11, K0e M4)

`git diff soak/<run>..HEAD` and `git diff soak/<run>` (working tree) over
`testing/k8s/demo/demo-tag.sh`'s input list (with and without its `*.md`
exclusion) and over `go python nodejs/packages dotnet`: **empty for all
three tags** (`soak/k8a-20261124b`, `soak/k8b-20261125`,
`soak/kshut-20261126c`), checked 2026-11-26 at HEAD `7252420`.
`demo-tag.sh` is unchanged since each tag and prints `7080d19` at each tag
and at HEAD. K-cal ran at `1d683b2` without a tag; the same paths are
unchanged from it to HEAD. The tags are on origin.

Running images (read-only `kubectl get deploy` and pod `imageID`,
2026-11-26, after `kshut-20261126c`): all twelve demo Deployments run tag
`7080d19` = `demo-tag.sh` at every soak tag. Digests were not recorded at run
time (K-D16 gap above). Harbor proof (D10 rule 1), read 2026-11-27 with
anonymous GETs of `/api/v2.0/projects/development/repositories/<repo>/artifacts/7080d19`:
for all twelve repositories the artifact digest equals the live one below,
and the `7080d19` tag's push time (2026-11-23T18:38:01Z … 18:51:53Z; the
`python-worker` artifact itself is from 2026-11-19, re-tagged 2026-11-23) is
before K8a started (`k8a-20261124b` `state.json` `started` =
2026-11-24T14:26:14Z). No mismatch. The digests:

| Image (`:7080d19`) | Digest |
|---|---|
| `otel-libs-demo-go-api` | `sha256:88ef3d34c2118f5f8d985e2d3ec839b823b85bbd0ea2c0dd79ffee3adc563c31` |
| `otel-libs-demo-go-backend` | `sha256:c3b7ea1d905301b7f8657c99d9d061b1d58d448d6b502bbb5b6d6e3472e7d4e6` |
| `otel-libs-demo-go-worker` | `sha256:b00431a47df468ac748bfc5709b392578f5abadd8a4bc37fc5520e9e70a85a2c` |
| `otel-libs-demo-python-api` | `sha256:e5b394b752356d2769aa6e2262ec769fd93c9cc38b9015873a8eb40e4feb774d` |
| `otel-libs-demo-python-backend` | `sha256:8a1e0ce4e8559fc632c29fbfb28148b31b19be37340ebf453cd4d2da7420f3cc` |
| `otel-libs-demo-python-worker` | `sha256:4de4df46cdf4a821ca115f0655fe74c9f402eae2761921829949b10c163689ee` |
| `otel-libs-demo-node-api` | `sha256:c7d88935477a09d04cbf04d013383b641653dd2bbe30c6f447b428a4dbdffc28` |
| `otel-libs-demo-node-backend` | `sha256:a8bacc17a43b846138840940f851c0cd5251a4dbec194b7a730a06b5385a7837` |
| `otel-libs-demo-node-worker` | `sha256:15ca078017ced14a6951fb3a5d0dce0bd0735057a1401074c48207477154cd9f` |
| `otel-libs-demo-dotnet-api` | `sha256:d720adc6ee6433b737393ff7c95e705aa2e203c45a2f1f5414b1a55677cc8d78` |
| `otel-libs-demo-dotnet-backend` | `sha256:ec35f5ff0a11b9d3e4670b245e59c817299823b58b6a3767a69eae408a2ecc89` |
| `otel-libs-demo-dotnet-worker` | `sha256:2f45bf09dc3497fa1afe627088a2839ea2d0c5bcfa52b6d5a2d383b3d080fd22` |

Outside the freeze paths, HEAD and the working tree differ from the tags in
tooling, CI and site config only: `testing/k8s/` (soak tools and tests),
`.github/workflows/ci.yml`, `.github/workflows/docs.yml`, `mkdocs.yml`,
`tools/regen_index.py`, `tools/tests/test_spec_tools.py`.

## Secret scan

`verify.scan_secret`, the report writer's patterns (auth value, URL
userinfo), each run's canary (read by the tools) and the fakeotlp redirect
token (`soakcount.FAKEOTLP_TOKEN`), case-insensitive, over the four reports,
this page and `sr10-gaps.json`: 0 hits (counts only), final run
2026-11-27T14:27Z.

## Contradictions with the recorded notes

Found at K11 and resolved by the owner's rulings of 2026-11-26 (dated notes
in requirements, tasks, config-contract D10 and A56):

1. AG13 existed only in tasks.md → recorded in requirements and D10 with
   AG14–AG17.
2. 41 SR10 FAIL items without an entry → 9 worker label-key items
   UNMEASURED (traffic rule); 32 span differences classified and ruled 2026-11-27 (table above).
3. A56 "stays FAIL" → the item is UNMEASURED in `k8b-20261125`; A56 row,
   K8b-x note and the allowlist note corrected.
4. The report's "Commit" and missing digests → tool fixed, reports
   regenerated.
5. D10 rule (2) vs. tooling and CI changes since the tags → rule corrected.
6. K8a start-of-window `verify.py demo` → accepted gap AG20 (owner
   2026-11-27). K1 24 h baseline (B1) → re-run 2026-11-27T14:10Z: NOT MET (a
   roll-list container start at 2026-11-26T19:01:51Z); re-run
   2026-11-27T19:05Z (`runs/k1-20261127b`): **met** — 24 h clean, 65/65.
