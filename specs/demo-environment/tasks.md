# Demo environment — tasks

**Status**: Draft r3.1 (2026-11-16). Spec: [requirements](requirements.md) ·
[design](design.md). Ids: tasks `DE<n>`, decisions `DE-D<n>`, owner questions
`DE-O<n>` (plain `D<n>` is taken by the config-contract design).

> **r2 (2026-11-16)**: owner rulings DE-O1…DE-O8 recorded below (questions
> closed); r1 review applied (design r2 note). Task changes: DE0 done, DE0b
> done (rulings), DE0c added (re-review of r2); DE5 gains the trace-matrix
> id pattern; DE8 creates real task ids; DE9 reduced to the Explore
> confirmation (the platform applied the requests); DE10 smoke only; DE12
> added (examples-replacement pass).
>
> **r3 (2026-11-16)**: DE0c done (`spec-refuter` re-review N1–N8 and an
> observability audit with live measurements, both applied — design r3
> note); DE0d added (re-review of r3); DE2 gains the SCRAM helper on stdin and
> the initdb Secret; DE5 gains the structured-metadata Loki scan, the
> `target_info` run join and the edge rules; DE9 gains the zero-padding check
> and the `^trace_id$` request. Coordinator ruling recorded below.
>
> **r3.1 (2026-11-16)**: DE0d done — r3 re-review found two HIGH (check c
> scenarios, g.sentinel split) and two MEDIUM; applied (design r3.1 note);
> DE0e added. The platform owner set the Loki derived field `matcherRegex:
> ^trace_id$` (homelab-gitops change); DE9 keeps the manual Explore check
> and drops the request.

Rollout order (ADR-004) applies to helper changes, not to this demo: the demo
changes no helper; the gaps it finds go to the tasks that own them (A36, A37,
and the new ids DE8 creates).

## Tasks

| Task | Depends on | Covers | Status |
|---|---|---|---|
| DE0 Adversarial review of r1 (`spec-refuter`) | — | format rule | ✅ 2026-11-16 (6 HIGH, 8 MEDIUM and LOW items; applied in r2) |
| DE0b Owner rulings DE-O1…DE-O8 | DE0 | DE-D1, DE-D10, DE-D12, DE-D14, DE-D15 | ✅ 2026-11-16 (below) |
| DE0c Re-review of r2 (`spec-refuter` N1–N8) and observability audit with live measurements (14:23–14:42 UTC) | DE0b | format rule | ✅ 2026-11-16 (applied in r3) |
| DE0d Re-review of r3 | DE0c | format rule | ✅ 2026-11-16 (2 HIGH, 2 MEDIUM, LOWs; applied in r3.1) |
| DE0e Confirm r3.1 fixes (spot check of H1, H2, M1, M2) | DE0d | format rule | ✅ 2026-11-16 (READY; two LOW wording gaps applied in r3.2) |
| DE1 Shared contract: `demo/proto/otellibs/demo/v1/demo.proto`, `demo/sql/schema.sql` (table + `BEFORE INSERT` trigger, DE-D4), `demo/README.md`; `go/demo/go.mod` with `replace` lines; Go stubs in `go/demo/gen/` + CI regeneration diff check (Docker) | DE0e | DR0, DE-D3 | ☐ |
| DE2 Backing services in `testing/k8s/demo/demo.sh` (`apply`, `delete`, `render`): PostgreSQL + Redis by digest, `emptyDir`, `redis.conf` Secret, SCRAM verifiers from a `python3` stdlib helper reading the password on stdin, verifier SQL in a Secret-mounted initdb script, `log_min_error_statement=panic` init, Secrets via `stringData` with `create -f -` / `apply --server-side`, `REDISCLI_AUTH` probes (DE-D13) | DE0e | DR1, DR11, DE-D4, DE-D13 | ☐ |
| DE3a Go apps `go/demo/{worker,api,backend}` (+ Dockerfiles, root context) | DE1 | DR0–DR5, DE-D5–DE-D9 | ☐ |
| DE3b Python apps `python/demo/…` | DE1 | same | ☐ |
| DE3c Node.js apps `nodejs/demo/…` (node-redis, `pg`) | DE1 | same | ☐ |
| DE3d .NET apps `dotnet/demo/…` (Npgsql, StackExchange.Redis, gRPC instrumentation key) | DE1 | same | ☐ |
| DE4 `testing/k8s/build-demo.sh` (root `git archive HEAD`, `linux/amd64`, `MIN_FREE_GB` guard as `build-examples.sh:44-69`) and the app manifests in `demo.sh` (split layout, Budget-table requests, pod label `otel-libs/run`, K-D2 pre-flight + `FailedScheduling` watch, `LANGS`, `RUN`) | DE2, DE3a–d | DR11, DE-D2, DE-D12 | ☐ |
| DE5 `verify.py demo` (checks a–g with span sets, minimums, settle delay, hygiene set, client-side Loki scan of line + structured metadata, `target_info` run join, `_bucket` exemplar selectors, `connection_type` edge rules, exit codes), `testing/k8s/demo/expected.json`; `tools/spec_lint.py` `xfail-task` rule extended to it; `tools/trace_matrix.py` `ID_PATTERN` (line 62, today `(?:SR|RT|JS|DN|R|G|P)\d+`) gains `DR`, with `tools/trace-allow.txt` entries for DR0–DR13 until tests cite them; Prometheus `query_range` client and `kubectl get -o json` wrapper shared with soak K1 | DE1 | DR5–DR12, DE-D11 | ☐ |
| DE6 Baseline run on `k8s01` (deploys only after 15:00 local time, the cluster's standing rule): all languages or rotation per pre-flight; replace every prediction of the design matrix with the measured cell in `expected.json`; record DB/Redis node names and `connection_type`, expired-edge count, DB span names, Prometheus start time | DE4, DE5 | Acceptance | ☐ |
| DE7 Calibrate requests from DE6 (`container_memory_working_set_bytes`, CPU rate per container); dated note in DE-D12 | DE6 | DE-D12 | ☐ |
| DE8 Gap filing: create `A38` (Go `ext/otelsql` passes no `db.namespace`, `go/ext/otelsql/sql.go:42-44`) and, if needed, `A39`… as rows in `specs/config-contract-v0.3/tasks.md` before `expected.json` cites them; link each `xfail` cell from its task; root `README.md` gap rows for anything new | DE6 | DR9 | ☐ |
| DE9 Manual Explore checks: the Loki `trace_id` derived field on an OTLP demo record (asked by the platform owner) and trace → logs from a trace id with leading zeros; the platform owner already set `matcherRegex: ^trace_id$` (DE-D14); results in the run report | DE6 | DR10, DE-D14 | ☐ |
| DE10 Optional local path `demo/compose.yaml`, smoke only, own Collector config → local Prometheus (DE-D15) | DE3a–d | DE-D15 | ☐ |
| DE11 Refutation of the DE6 report (`observability` + `dev`), `code-review` of the diff, CHANGELOG (Unreleased), `demo/README.md` + `testing/k8s/README.md` section | DE6–DE9 | change loop | ☐ |
| DE12 Examples-replacement pass: the demo apps replace each language's example `api` / `backend` / `process`; `custom-signals` stay; CI `example-images` matrix and the Node.js `Dockerfile.demo` version check moved; `mkdocs.yml`, example READMEs/EXAMPLES.md, AGENTS.md rule "api, backend, worker" | DE11 | DR13, DE-D1 | ☐ |

**Promotion triggers for deferred work**
- A Redis error scenario (failed command) is added when A36's Redis scope is
  ruled (`specs/config-contract-v0.3/tasks.md:58`).
- DE10 is dropped in DE12 if nobody has used it.
- If the platform turns exemplar storage off, check b goes BLOCKED and the
  owner rules again (DE-D10 reversal).

## Owner rulings (2026-11-16) — questions closed

| Id | Question | Ruling |
|---|---|---|
| DE-O1 | Replace the examples' api/backend/process, or stay separate? | **Separate now**; the demo replaces the examples' api/backend/process in a later, separately named pass (DE12; A13 is done); `custom-signals` stay. |
| DE-O2 | Exemplar storage on `kps` and the Grafana exemplar link? | **Asked and applied** by the platform owner (homelab-gitops change): `kps` runs `--enable-feature=exemplar-storage`, `exemplars.maxSize: 100000` (in memory, empty after each Prometheus restart); Grafana Prometheus datasource `exemplarTraceIdDestinations` → `tempo`, label `trace_id`. Check b is a gate; no Compose fallback (validate on the cluster). Verified read-only 2026-11-16 (DE-D10, DE-D14). |
| DE-O3 | Tempo trace-to-logs on structured metadata? | **Asked and applied**: `customQuery` `{service_name="${__span.tags["service.name"]}"} \| trace_id="${__trace.traceId}"`, `filterByTraceID` removed. The Loki derived field stays label-type on `trace_id`; matching OTLP structured metadata is `unmeasured` — confirm in Explore (DE9). |
| DE-O4 | PostgreSQL / Redis images: Docker Hub or a harbor mirror? | **Docker Hub, pinned by digest**; mirror only if a pull fails. (`examples.sh:114` pins `curlimages/curl` by tag only; the demo pins by digest.) |
| DE-O5 | Local Compose path? | **Optional (DE10), no acceptance there.** |
| DE-O6 | Instrumentation exception-event gaps: show or suppress? | **Show as strict `xfail` cells** (A36/A37). |
| DE-O7 | Go stack trace source? | **`debug.Stack()` at the log site**, documented as the logging goroutine's stack. |
| DE-O8 | All four languages at once? | **Yes when the pre-flight fits; rotation is the fallback.** |

**Coordinator ruling (2026-11-16, N3 / audit #12):** the PostgreSQL edge
requires `connection_type="database"`; Go stays `xfail:A38` (`virtual_node`,
server `postgresql`); Python, Node.js and .NET cells are real predictions
(Node.js `database`, server `demo_node`; Python probably `database`, server
`demo_python`, `unmeasured`).

No owner question is open in r3.
