# Demo environment — requirements

**Status**: Draft r3.1 (2026-11-16); r1 reviewed (DE0 ✅), r2 re-reviewed and
audited (DE0c ✅); r3 not yet re-reviewed (DE0d) | **Owner**: repo owner | **Relates to**:
[ADR-014](../decisions/ADR-014-exception-detail-in-correlated-logs.md)
(exception detail in correlated logs),
[ADR-004](../decisions/ADR-004-phased-rollout-go-python-first.md),
[ADR-007](../decisions/ADR-007-keep-prometheus-in-core.md),
[ADR-008](../decisions/ADR-008-shared-parity-fixture.md) (strict xfail),
[ADR-002](../decisions/ADR-002-parentbased-sampler.md),
[soak-simulation Phase K](../soak-simulation/design.md) (K-D1…K-D9),
[config-contract-v0.3](../config-contract-v0.3/tasks.md) A36, A37.
Design: [design.md](design.md) · Tasks: [tasks.md](tasks.md).

> **r2 (2026-11-16)** after the r1 review (`spec-refuter`: six HIGH, eight
> MEDIUM and the LOW items of that review) and the owner rulings of the same day (tasks.md
> "Owner rulings"). Changes here: requirements rewritten in WHEN … THEN …
> SHALL form; minimum-count rules so no check passes on empty data (H2);
> status messages, bind parameters and a trigger-raised duplicate (H3); check
> d per span set (H4); hygiene over every span of the run plus a 48-hex
> token rule and a Loki scan (H5); credential handling (H6); exemplars are a
> gate again — the platform owner enabled exemplar storage (ruling DE-O2);
> `otel_libs.run` named as a test-only exception to principle 4; A33 dropped
> as a gap owner (the demo never sets the exemplar filter). r1 text is
> replaced in place; the r1 file was not committed.
>
> **r3 (2026-11-16)** after the `spec-refuter` re-review of r2 (N1–N8) and
> an observability audit with live measurements (14:23–14:42 UTC, Go
> examples only). Changes here: DR3 — trigger fires on `dup-%` ids, the
> exception messages carry run tokens distinct from the constant gRPC
> messages, a client span's constant gRPC description is allowed, the
> owning span logs before the rethrow; DR6 — exemplars read from `_bucket`
> series, run scoped by `target_info`; DR8 — the PostgreSQL edge requires
> `connection_type="database"` (coordinator ruling), HTTP/gRPC edges have
> no `connection_type`; DR12 — Loki scan over structured metadata, scoped to
> the run's pods, anchored token; severity compared by number.
>
> **r3.1 (2026-11-16)** after the r3 re-review (DE0d: two HIGH, two
> MEDIUM): DR7 — check c requires `ok`, `business_error`, `db_error` only
> (`unhandled` never reaches the backend); DR3 — the framework maps the
> unhandled exception to 500 and may log it itself (not checked); DR12 —
> the Loki rule is about the 48-hex token only (run tokens in logs are
> intended, ADR-014).

## Context

Owner request (2026-11-16, translated): *"Above all: log/trace correlation,
exemplars, custom metrics, the error in the trace and the stack trace in the
log. We need a demo environment to validate it: worker → api → grpc, and in
between a database / queue and whatever else can be validated. The same
database and queue can be shared by all languages if that simplifies."*

What exists today cannot show this (read 2026-11-16):

- The example apps use no database, cache or queue. The .NET backend fakes
  its DB span by hand with the legacy key `db.system` and a `Task.Delay`
  (`dotnet/example/dotnet-backend/Services/OrderGrpcService.cs:40-49`); the
  .NET queue consumer fakes SQS attributes with the pre-1.24 key
  `messaging.destination` (`dotnet/example/dotnet-process/QueueConsumerWorker.cs:51-53`).
- Four domains and three protos (`go/example/protos/anomaly.proto`,
  `python|nodejs/example/protos/notification.proto`,
  `dotnet/example/Protos/order.proto`); Go has no gRPC hop
  (`go/example/protos/anomaly.proto:1-4`, `go/example/go-backend/main.go:1-10`)
  and `testing/k8s/examples/examples.sh:108-131` drives `go-api` with `curl`.
- Go `go-api` / `go-backend` log with the standard library
  (`go/example/go-api/main.go:33,135`; deferred row
  `specs/config-contract-v0.3/tasks.md:104`).
- The Go, Python and Node.js custom-signals examples put the exception
  message on the span (`go/example/go-custom-signals/main.go:193`,
  `python/example/python-custom-signals/custom_signals.py:136`,
  `nodejs/example/node-custom-signals/custom-signals.js:125`); only .NET
  follows ADR-014 (`dotnet/example/dotnet-custom-signals/Program.cs:184-192`).
- `verify.py` checks service-graph edges and one log → trace lookup per
  language (`testing/k8s/verify.py:248-302`); no exemplar, error-route,
  custom-metric or trace-structure check.
- Exemplar storage: disabled when r1 was written (`enable-feature` empty,
  measured ≈ 13:45 UTC); enabled by the platform owner the same day
  (ruling DE-O2; read-only check ≈ 14:20 UTC: `kps` CR
  `spec.enableFeatures: ["exemplar-storage"]`, `spec.exemplars.maxSize:
  100000`, flag `enable-feature=exemplar-storage`, Prometheus start time
  2026-11-16T14:14:31Z). Exemplars live in memory: a Prometheus restart
  empties them.

## Glossary

- **Demo language set**: the apps `worker`, `api`, `backend` of one language,
  services `demo-<lang>-<app>`, `<lang>` ∈ `go`, `python`, `node`, `dotnet`.
- **Scenario**: `ok`, `business_error`, `db_error`, `unhandled` (design
  "Scenario table").
- **Run**: one apply with run id `<run>`, carried as the resource attribute
  `otel_libs.run=<run>` and the pod label `otel-libs/run=<run>`.
- **Window**: `[now − settle − W, now − settle]`, settle = 2 min (the
  minimum, DE-D11), W = 15 min by default.
- **Owning span**: the span that represents the failing operation of a
  scenario (design "Scenario table").
- **Demo span**: a span created by demo code, tracer (instrumentation scope)
  name `otel-libs-demo`.
- **Expected-results matrix**: `testing/k8s/demo/expected.json`, language ×
  check item → `pass` | `xfail:<task>` | `n/a:<reason>` (DE-D11).

## Requirements

### DR0 — One topology, four languages
- WHEN the demo is applied for a language THEN it SHALL run a `worker` (load
  driver and queue consumer), an `api` (HTTP server, gRPC client, Redis cache
  client, queue producer) and a `backend` (gRPC server, PostgreSQL client)
  with the routes, RPCs, scenario sequence, span names, instrument names,
  units, attributes and log messages of design DE-D2, DE-D3, DE-D7, DE-D9,
  identical in every language.
- WHEN the api calls the backend THEN the call SHALL be gRPC in every language,
  Go included, through the helpers' public gRPC API (Go
  `otelhelper.GRPCServerOption` / `GRPCDialOption`, `go/middleware.go:79,85`).
- WHEN a demo app is built THEN it SHALL take its RPC definitions from the one
  shared `.proto` (DE-D3) and use the helpers only through their public API and
  the SQL and Redis extensions (Go `ext/otelsql`, `ext/otelredis`; Python
  `[sql]`, `[redis]`; Node.js `@staffops/otel-helper-sql`, `-redis`; .NET
  `StaffOps.OtelHelper.Sql`, `.Redis`); the Go apps SHALL be their own module
  (`go/demo/go.mod` with `replace` lines to the local helper modules), so the
  core module gains no dependency and its coverage gate is unchanged (DE-D3).
- WHEN a demo app starts THEN it SHALL set no resource attribute in code and
  take every setting from environment variables. `service.name` and
  `deployment.environment.name` come from `OTEL_SERVICE_NAME` and
  `OTEL_RESOURCE_ATTRIBUTES` (principle 4); the one other resource attribute,
  `otel_libs.run`, is a **test-only exception** to principle 4, set through
  `OTEL_RESOURCE_ATTRIBUTES` as the smoke Jobs do (`testing/k8s/README.md:71-76`).

### DR1 — Shared backing services
- WHEN the demo runs THEN one PostgreSQL and one Redis SHALL serve every
  language in `dev-otel-libs`: one database and one role per language, one key
  prefix and one stream per language (DE-D4).
- WHEN the backend writes THEN it SHALL use bind parameters for every value
  (never a literal in the statement text) and SHALL keep at most a fixed
  number of rows per language; data SHALL be ephemeral (no PersistentVolumeClaim).

### DR2 — Queue with manual W3C propagation
- WHEN the api finishes an `ok` order THEN it SHALL publish one event to
  `demo:<lang>:orders` inside a PRODUCER span, injecting the current context
  with the global propagator (W3C TraceContext + Baggage) into the message
  fields (DE-D5, DE-D6).
- WHEN the worker processes a message THEN it SHALL extract the context and
  start one CONSUMER span with **one link** to the extracted context and the
  extracted context as **parent**, and SHALL acknowledge the message inside
  that span (DE-D6).
- WHEN producer and consumer spans are created THEN they SHALL follow the
  messaging conventions of the schema the helpers pin, `1.43.0` (Go
  `go/resource.go:18`; Python `python/staffops_otel_helper/setup.py:200`;
  Node.js `nodejs/packages/otel-helper/src/internal/setup.ts:202`; .NET
  `dotnet/StaffOps.OtelHelper/ResourceSetup.cs:31`): span name
  `{messaging.operation.name} {destination}`, kinds PRODUCER / CONSUMER, and
  the attribute set of DE-D6.

### DR3 — Deterministic error routes
- WHEN the worker runs THEN it SHALL drive the fixed scenario cycle of DE-D7,
  so every window holds a known minimum of each scenario.
- WHEN `business_error` runs THEN the backend's business code SHALL raise
  `OrderRejectedError("amount over limit rej-<run>")`, return gRPC
  `FAILED_PRECONDITION` with the constant message `order rejected`, and the
  api SHALL answer `422`.
- WHEN `db_error` runs THEN the backend SHALL insert the row `dup-<run>`; a
  `BEFORE INSERT … FOR EACH ROW WHEN (NEW.id LIKE 'dup-%')` trigger SHALL
  raise `SQLSTATE 23505` unconditionally with the primary message
  `duplicate order dup-<run>` (no seed row, DE-D4); the backend SHALL raise
  `OrderExistsError("order dup-<run> exists")`, return `ALREADY_EXISTS` with
  the constant message `order exists`, and the api SHALL answer `409`.
- WHEN `unhandled` runs THEN the api SHALL throw
  `DemoUnhandledError("boom unh-<run>")` inside its demo span
  `demo.order.accept`, which records and logs it (next item) and rethrows;
  the framework's default handling (Go: the api's recovery middleware) SHALL
  map it to `500`; the framework's own error log of it (Kestrel, uvicorn's
  "Exception in ASGI application") may also appear and is not checked.
- WHEN an error scenario runs THEN its **owning demo span** SHALL have status
  ERROR without description, `error.type` = the pinned type name
  (`OrderRejectedError`, `OrderExistsError`, `DemoUnhandledError`), and at most
  one `exception` event carrying only `exception.type` (ADR-014 decision 1);
  and the demo code SHALL emit one ERROR log record **while that span is
  current**, before any rethrow, with the message as body and
  `exception.type`, `exception.message`, `exception.stacktrace` (ADR-014
  decision 2) (DE-D8). The unqualified type literals are a deliberate
  deviation from the semconv recommendation of a fully qualified name (DE-D8).
  *Corrected 2026-11-27 (DE11): "at most one `exception` event" is stale. Since
  A45 (contract design D15; DE-D8 note of 2026-11-17) the owning span carries **no**
  `exception` event in any language, and check d.demo fails on any event, a
  type-only one included (`testing/k8s/verify.py` `_d_demo`).*
- WHEN the worker's `demo.drive` span receives a non-2xx answer THEN it SHALL
  set status ERROR with `error.type` = the HTTP status code and
  `demo.outcome=error`, record no `exception` event (no exception occurs in the
  worker), and log one WARN record `order request failed` with
  `http.response.status_code` (DE-D8).
- WHEN an error scenario runs THEN the spans created by instrumentations
  (gRPC client and server, HTTP server and framework, DB, Redis) SHALL be
  checked per span set against ADR-014 and against the semconv status rules
  (gRPC server span UNSET for `FAILED_PRECONDITION` and `ALREADY_EXISTS`,
  client span ERROR, whose description MAY be the constant gRPC message:
  `specs/config-contract-v0.3/design.md:166`,
  [semconv v1.43.0 gRPC](https://github.com/open-telemetry/semantic-conventions/blob/v1.43.0/docs/rpc/grpc.md));
  each set has its own expected cell, and a violation is an `xfail` naming
  the task that owns it (A36 DB clients, A37 HTTP/gRPC), never hidden (DR9).
  A span leaks detail when it holds one of the run tokens `dup-<run>`,
  `rej-<run>`, `unh-<run>`, an `exception.message`, an
  `exception.stacktrace` or a `grpc.error_message`.

### DR4 — Custom signals
- WHEN the backend handles an order THEN, inside the active sampled demo span
  `demo.order.process`, it SHALL record the counter `demo.orders.processed`
  (unit `{order}`) and the histogram `demo.order.duration` (unit `s`, the
  DE-D9 boundaries) with `demo.scenario`, `demo.outcome` and, on failure only,
  `error.type`.
- WHEN an `ok` order is processed THEN the backend SHALL log one INFO record
  `order processed` with `order.id` and `demo.scenario`, and the worker SHALL
  log one INFO record `order event processed` inside its CONSUMER span.

### DR5 — Log/trace correlation
- WHEN a demo app logs through the helper inside a sampled span THEN the Loki
  record SHALL carry that span's `trace_id` and `span_id`, the trace SHALL
  resolve in Tempo, and the `span_id` SHALL name a span of that trace (check a).

### DR6 — Exemplars
- WHEN the demo runs THEN it SHALL leave `OTEL_METRICS_EXEMPLAR_FILTER` unset
  (spec default `trace_based`,
  [OTel SDK environment variables](https://opentelemetry.io/docs/specs/otel/configuration/sdk-environment-variables/#exemplar)).
- WHEN check b runs THEN the exemplars on the `_bucket` series of
  `http.server.request.duration`, the RPC server duration, the DB client
  duration and `demo.order.duration`, for the run's instances (from
  `target_info{otel_libs_run="<run>"}`), SHALL carry trace ids that resolve
  in Tempo and span ids that are spans of those traces, per language and per metric the
  language emits (design "Metric map"); a metric a language does not emit, or
  a language whose SDK records no exemplars (Node.js, root `README.md:329`),
  is an `n/a:<reason>` cell (DE-D10).
- WHEN the pre-flight finds `exemplar-storage` absent from the Prometheus
  flags THEN check b SHALL report BLOCKED, which fails the run (DE-D11).

### DR7 — Custom metrics after translation
- WHEN check c runs THEN, for every language and each of the scenarios `ok`,
  `business_error`, `db_error` (`unhandled` fails in the api before the gRPC
  call, so the backend records nothing for it; a backend series with
  `demo_scenario="unhandled"` is UNEXPECTED), the custom
  metrics SHALL exist for the run's backend instance (joined on
  `target_info{otel_libs_run="<run>"}`) with `increase(…[W]) > 0`, and the
  names, labels, bucket bounds and series structure (counter `_total`;
  histogram `_bucket` with `le="+Inf"`, `_sum`, `_count`) that
  `UnderscoreEscapingWithSuffixes` produces (`demo_orders_processed_total`,
  `demo_order_duration_seconds_{bucket,count,sum}`; labels `demo_scenario`,
  `demo_outcome`, `error_type`; soak K-D9).

### DR8 — Trace structure and service graph
- WHEN check e runs THEN at least one `ok` trace that started before the
  window end SHALL contain the expected spans and relations of design
  "Expected trace", including the DB and Redis client spans, and the CONSUMER
  span's link and parent SHALL both point to the PRODUCER span.
- WHEN check e runs THEN the gateway `service_graph` connector SHALL show, for
  the window, the edges worker → api, api → backend, api → worker
  (`connection_type="messaging_system"`, ≥ 0.8 × the expected `ok` orders),
  backend → the PostgreSQL node with `connection_type="database"`
  (coordinator ruling 2026-11-16; Go `xfail:A38`), and api → the Redis node
  (server value recorded); HTTP and gRPC edges carry no `connection_type`
  (DE-D11).

### DR9 — Parity and gaps
- WHEN `verify.py demo` finishes THEN it SHALL print one matrix language ×
  check item; a failing cell SHALL cite the file:line or upstream reference
  that explains it; a known gap SHALL be an `xfail:<task>` naming an open task
  in an active `tasks.md`; an `xfail` cell that passes SHALL be reported XPASS
  and fail the run (strict, as ADR-008); an `n/a` cell SHALL be strict too —
  data present where `n/a` is expected is reported as UNEXPECTED and fails.

### DR10 — Grafana correlation settings (read-only)
- WHEN a run report is written THEN it SHALL record the Grafana datasource
  settings the demo relies on (Loki derived field → Tempo, Tempo trace-to-logs,
  Prometheus exemplar → Tempo), read without credentials beyond a viewer,
  and SHALL say whether the Loki derived field opens the trace from an OTLP
  record in Explore (manual step, DE-D14). Changing a setting is a request to
  the platform owner.

### DR11 — Cluster constraints and credentials
- WHEN the demo writes to the cluster THEN it SHALL write only namespaced
  objects in `dev-otel-libs` with `kubectl --context k8s01`, and SHALL NOT
  write to `opentelemetry`, `monitoring`, Grafana or any cluster-scoped object
  (soak SR15 scope, without its K4 label exception).
- WHEN images are built THEN they SHALL come from the committed HEAD and be
  pushed to `harbor.staffops.space/development` (as
  `testing/k8s/build-examples.sh:40-42`).
- WHEN a run starts THEN every container SHALL have explicit CPU and memory
  requests and a memory limit, and the soak K-D2 pre-flight SHALL decide
  whether the four languages run together or one per window (DE-D12); pods
  SHALL be single-container (soak K-D4).
- WHEN the backing services are created THEN the PostgreSQL and Redis
  passwords SHALL be generated for their lifetime (`demo.sh apply` →
  `demo.sh delete --all`; language rotation windows reuse them) and SHALL
  never appear in a command line, an object annotation, stdout, a container
  log, a log record, a span or a report; they SHALL be deleted with the
  backing services (DE-D13).
  *(r3.3, 2026-11-16: was "per run … deleted with the run"; the DE2 review
  found that a rotation window cannot regenerate them — PostgreSQL keeps the
  roles' verifiers in the emptyDir initialized at the first apply.)*

### DR12 — Hygiene over every span and log of the run
- WHEN check g runs THEN no span of the run (design DE-D11 defines the set,
  connection-time root traces included) SHALL contain, in any attribute,
  event attribute or status message: an anchored 48-hex token
  `(^|[^0-9a-f])[0-9a-f]{48}([^0-9a-f]|$)` (the shape of the generated
  passwords), `password=` (case-insensitive), a URL with userinfo
  (`://[^/@]+:[^@]+@`), or a run token (`dup-<run>`, `rej-<run>`,
  `unh-<run>`); and no Loki record of the run's pods or of the
  `demo-postgres` / `demo-redis` pods over the run SHALL contain the 48-hex
  token, in its line or in any structured-metadata field (run tokens in log
  records are intended: ADR-014 puts the exception detail there). The check SHALL print
  counts and labels only, never the matched text.
  *Corrected 2026-11-27 (DE11): "connection-time root traces included" is stale.
  The connection-time TraceQL search was replaced by the start-up scan (design
  DE-D11, correction M7 of 2026-11-18, owner option A): the span set and the Loki
  scan cover the run window plus the first 300 s after each pod start, which holds
  the connection-time roots without a name filter.*

### DR13 — Relation to the examples and docs
- WHEN the demo is added THEN the existing example apps, their Dockerfiles,
  the CI `example-images` job (`.github/workflows/ci.yml:625-680`) and the
  MkDocs navigation (`mkdocs.yml:48-66`) SHALL stay unchanged; the
  replacement of the examples' api/backend/process apps is the separate
  examples-replacement pass (task DE12, ruling DE-O1).
- WHEN the demo is added THEN the same change SHALL add `demo/README.md`, a
  `testing/k8s/README.md` section and a CHANGELOG (Unreleased) entry.

## Checks (summary; queries and minimums in design DE-D11)

| Id | Check | Minimum (no pass on empty data) |
|---|---|---|
| a | Demo OTLP Loki records with a sampled `trace_id` resolve in Tempo, `span_id` in the trace | ≥ 1 record per demo service; total ≥ half the cycle's expected records |
| b | Exemplars on the `_bucket` series resolve in Tempo (trace and span) | ≥ 1 exemplar per (language, metric) cell that is not `n/a` |
| c | Custom metrics: names, labels, bounds, structure, run instance | `increase(…[W]) > 0` per language and backend scenario (`ok`, `business_error`, `db_error`; r3.2) |
| d | Error routes, per span set | ≥ 1 trace per error scenario per language |
| e | Trace structure and service-graph edges | ≥ 1 `ok` trace older than the settle delay; `increase > 0` per edge; messaging ≥ 0.8 × expected |
| f | Matrix, strict xfail and n/a | — |
| g | Hygiene (DR12) | the span set is non-empty |

## Acceptance

- [ ] `verify.py demo --run <run>` exits 0 for all four languages: every
      cell `pass`, or `xfail:<task>` / `n/a:<reason>` as the
      expected-results matrix says, with no XPASS, UNEXPECTED or BLOCKED.
- [ ] Check b passes for every non-`n/a` cell on the cluster (no local
      fallback, ruling DE-O2).
- [ ] The Grafana settings snapshot and the manual Explore result for the Loki
      derived field are in the run report (DE-D14).
- [ ] Report refuted by `observability` + `dev` agents; `code-review` of the
      diff; CHANGELOG entry.

## Out of scope

- Changing the platform (Collector CRs, Prometheus, Grafana, Tempo, Loki,
  Istio, Kyverno); needed changes are requests (DE-D10, DE-D14).
- Implementing A36 or A37; the demo measures them as `xfail` cells.
- Load testing, soak durations, failure injection (soak Phase S, K8, K10).
- A messaging extension for the helpers (none exists, DE-D5).
- Tail sampling (the cluster gateway has none, soak K-D1).
- Acceptance on the local Compose path (DE-D15: optional, smoke only).
