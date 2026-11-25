# Demo environment — design

**Status**: Draft r3.1 (2026-11-16); r1 reviewed (DE0), r2 re-reviewed by
`spec-refuter` and audited by an observability review (DE0c); r3 not yet
re-reviewed (DE0d). Requirements: [requirements.md](requirements.md) ·
Tasks: [tasks.md](tasks.md).

> **r2 (2026-11-16).** Owner rulings DE-O1…DE-O8 recorded (tasks.md) and
> the r1 review applied. Decisions changed: DE-D1 (ruling), DE-D4 (trigger,
> bind parameters, connection-time logging), DE-D5 (citation), DE-D6
> (corrected: the connector pairs by links), DE-D8 (pinned types, owning
> span, `demo.drive` rule, gRPC instrumentation key), DE-D10 (exemplar
> storage enabled; Compose fallback dropped; Node.js `n/a`), DE-D11 (span
> sets, minimums, settle delay, hygiene set, BLOCKED exit), DE-D12 (new
> reads), DE-D13 (credential handling), DE-D14 (settings applied by the
> platform), DE-D15 (smoke only). Each changed decision carries its own
> dated note.

> **r3 (2026-11-16).** Merges the `spec-refuter` re-review of r2 (N1–N8)
> and an observability audit with live measurements (14:23–14:42 UTC,
> Prometheus 3.16.0, Tempo 3.1.1, Grafana 13.3.1; only the Go examples were
> running, so live evidence is Go-only unless stated). Coordinator ruling
> (N3): the PostgreSQL edge must have `connection_type="database"`; Go stays
> `xfail:A38`. Decisions changed: DE-D4 (trigger fires on `dup-%` without a
> seed; per-language service-graph nodes), DE-D5 (citation pinned), DE-D8
> (log inside the owning span before rethrow; run-token exception messages;
> semconv deviation stated; per-language gRPC calls pinned), DE-D10
> (`_bucket` selectors, run scoping by `target_info`, Go measured end to
> end), DE-D11 (Loki scans include structured metadata, pod-scoped and
> anchored hygiene, `connection_type=""` edges, no `expired_edges`,
> structural metric check, severity number), DE-D13 (SCRAM tool), DE-D14
> (derived-field regex). New reference section "Id encodings". Each changed
> decision carries an r3 note.

> **r3.1 (2026-11-16)** after the r3 re-review (DE0d): check c scenarios
> (H1); g.sentinel split into `.db` and `.app`, fed by the server-side
> TraceQL counts (H2); Python aio gRPC server records `AbortError` on
> `context.abort` (M1); framework error logs not checked (M2); Grafana
> source version, `file_log` `service_name`, Loki derived field now
> `^trace_id$` (LOW, platform change). Notes marked "r3.1" in DE-D8,
> DE-D11, DE-D14 and the matrix.

Measurements: 2026-11-16, read-only against context `k8s01` (`kubectl get`,
`kubectl get --raw` through the API server service proxy, `kubectl describe
node`) and read-only Grafana datasource reads. r1 reads ≈ 13:40–14:00 UTC,
r2 reads ≈ 14:20 UTC. Upstream source read for r2: servicegraph connector
v0.165.0 (`connector/servicegraphconnector/connector.go`,
`internal/store/store.go`), XSAM `otelsql` v0.44.0 (`instruments.go`,
`utils.go`, `conn.go`), StackExchange.Redis 2.6.122
(`src/StackExchange.Redis/Interfaces/IDatabase.cs`).

Ids: decisions `DE-D<n>`, tasks `DE<n>`, owner questions `DE-O<n>`; plain
`D<n>` names the config-contract design decisions cited here.

## Topology (reference)

```
           per language <lang> ∈ go, python, node, dotnet (namespace dev-otel-libs, one container per pod)

 demo-<lang>-worker ──HTTP POST /orders──▶ demo-<lang>-api ──gRPC demo.v1.OrderService/PlaceOrder──▶ demo-<lang>-backend
   │  root span demo.drive                  │  Redis GET/SET (cache, -redis ext)                         │ span demo.order.process
   │                                        │  PRODUCER "send demo:<lang>:orders" + XADD                 │ PostgreSQL INSERT/SELECT (-sql ext)
   │                                        ▼                                                            ▼
   └◀── XREADGROUP (poll) ── Redis stream demo:<lang>:orders        shared demo-redis              shared demo-postgres (db demo_<lang>)
        CONSUMER "process demo:<lang>:orders" (link + parent = producer) + XACK

 every app ──OTLP gRPC──▶ otel-agent-collector.opentelemetry:4317 (k8s_attributes, filter/noise)
           ──▶ otel-gateway (span_metrics, service_graph) ──▶ Tempo · Loki · Prometheus (kps, exemplar storage)
 verify.py demo (host, read-only, API server service proxy) ──▶ Tempo / Loki / Prometheus / kube API
```

## Decisions

### DE-D1 — A separate demo now; it replaces the examples' api/backend/process in a later pass
**Choice**: new apps under `<lang>/demo/{worker,api,backend}` and
`testing/k8s/demo/demo.sh`; the examples, their Dockerfiles, the CI
`example-images` job and the MkDocs navigation are not touched. A later,
separately named **examples-replacement pass** (task DE12; A13 is already
done, `specs/config-contract-v0.3/tasks.md:60`) replaces each language's
example `api` / `backend` / `process` apps by the demo apps, keeps the
`custom-signals` examples, moves the CI `example-images` matrix and the
Node.js `Dockerfile.demo` version check (`.github/workflows/ci.yml:572`) to
the demo apps, updates `mkdocs.yml` and changes the AGENTS.md rule to
"api, backend, worker".
*(r2, 2026-11-16: owner ruling DE-O1 — separate now, replace later in a pass
of its own; r1 had placed the replacement in A13, which is done.)*
**Justification**: the examples are released documentation in the MkDocs nav
(`mkdocs.yml:48-66`) and CI builds twelve example images
(`.github/workflows/ci.yml:625-680`); changing them while the demo is unproven
moves CI and docs at once. The examples cannot be extended in place cheaply:
four domains, three protos, Go without gRPC (`go/example/go-backend/main.go:1-10`).
**Trade-offs**: two app sets until DE12.
**When it would be wrong**: fixes land only in the demo and the examples rot
before DE12.
**Reversal action**: run DE12 immediately after DE11.
**Alternatives**: replace now (CI/docs churn); extend the examples (four
domains stay).

### DE-D2 — Service names, layout and wiring
**Choice**: services `demo-<lang>-worker|api|backend`; one Deployment (+
Service for api and backend) per app, one container per pod; env
`OTEL_SERVICE_NAME`,
`OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=dev,otel_libs.run=<run>`,
`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-agent-collector.opentelemetry.svc.cluster.local:4317`,
`OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, `OTEL_METRIC_EXPORT_INTERVAL=10000`;
.NET backend also `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true`,
without which its gRPC server spans are not RPC-shaped (root `README.md:263-266`,
`dotnet/HOW-TO.md:205-212`). Pod labels as `examples.sh`
(`app.kubernetes.io/component=demo`, `otel-libs/lang`) plus
`otel-libs/run=<run>`; pod hardening as `testing/k8s/examples/examples.sh:95-106,165-173`.
*(r2, 2026-11-16: added the .NET gRPC key (review M4) and the run pod label
(H2: check c scopes by the run's pods).)*
**Justification**: single-container pods get `k8s.container.name` (soak K-D4;
compact pods get none, `testing/k8s/README.md:211`); every hop crosses
ztunnel (soak K-D2); `otel_libs.run` follows the smoke Jobs
(`testing/k8s/README.md:71-76`) and is the test-only exception to principle 4
(DR0).
**Trade-offs**: 14 pods; a 10 s export interval triples the export load of
the 30 s default (size `unmeasured`).
**When it would be wrong**: pod slots or requests stop fitting (DE-D12).
**Reversal action**: `LAYOUT=compact` as in `examples.sh` (loses
`k8s.container.name` and the ztunnel hops, recorded) or one language per window.
**Alternatives**: compact layout; load from outside the cluster (soak K-D2).

### DE-D3 — One shared contract under `demo/`; Go stubs committed; a separate Go module
**Choice**: `demo/proto/otellibs/demo/v1/demo.proto` (package `demo.v1`,
`OrderService` with `PlaceOrder`, `GetOrder`), `demo/sql/schema.sql`
(per-language table and the DE-D4 trigger, applied by the backend at start),
`demo/README.md`. Python generates stubs at image build (`grpcio-tools`),
Node.js loads the proto at run time (`@grpc/proto-loader`, as
`nodejs/example/node-api/main.js:84`), .NET uses `Grpc.Tools` at build, Go
commits `*.pb.go` under `go/demo/gen/` with a CI regeneration diff check in
Docker. The Go apps are one module `go/demo/go.mod` with `replace` lines to
`../` and `../ext/otelsql`, `../ext/otelredis` (as the examples' modules
replace the helper, `go/example/go-api/go.mod:57`), so the core
`go.mod` gains no gRPC-gen, pgx or go-redis dependency and the core coverage
gate does not see the demo. Demo images build from the repo root (`git
archive HEAD`) with a `.dockerignore` per Dockerfile.
*(r2, 2026-11-16: separate Go module made explicit, review M6.)*
**Justification**: one proto removes the cause of today's three domains; the
examples' language-directory contexts (`testing/k8s/build-examples.sh:7-10,75`)
cannot see a root `demo/`; committing stubs is the Go convention. "Core stays
lightweight" (AGENTS.md Packaging) holds because the demo is its own module.
**Trade-offs**: larger build contexts (`unmeasured`); proto changes require a
Go regeneration (CI enforces it).
**When it would be wrong**: a language's packaging check tars only `<lang>/`.
**Reversal action**: per-language byte-identical copies of the proto with a CI
equality check.
**Alternatives**: `buf` remote generation; proto under `testing/k8s/demo/`.

### DE-D4 — Shared PostgreSQL and Redis, ephemeral, bounded, duplicate raised by a trigger
**Choice**: `demo-postgres` (`postgres:17.11-alpine`, the version of the .NET
Sql tests, `dotnet/README.md:618`) and `demo-redis` (`redis:7.4.12-alpine`,
the version of the .NET Redis tests, `dotnet/README.md:587`), pinned **by
digest** at DE2, data on `emptyDir`. PostgreSQL: database and role
`demo_<lang>` per language (DE-D13 for credentials); table
`orders(id text primary key, customer text, amount numeric, scenario text,
updated_at timestamptz)`; every value passed as a bind parameter. `ok`
upserts id `ok-<seq mod 500>` (`INSERT … ON CONFLICT (id) DO UPDATE`) then
`SELECT`s it. `db_error` inserts `dup-<run>`; the trigger
`CREATE TRIGGER orders_dup BEFORE INSERT ON orders FOR EACH ROW WHEN (NEW.id LIKE 'dup-%') EXECUTE FUNCTION orders_raise_dup()`
raises **unconditionally**
(`RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = format('duplicate order %s', NEW.id)`);
no seed row is needed, so no insert of a `dup-` id ever succeeds and every
`db_error` fails the same way. The sentinel is in the **primary** error
message for every driver (pgx, Npgsql and node-postgres expose the primary
message; `DETAIL` is not relied on). Redis: `requirepass` (DE-D13); keys
`demo:<lang>:quote:<customer>` (TTL 30 s) and stream `demo:<lang>:orders`
(`MAXLEN ~ 1000`).
Clients: Go `pgx/v5` stdlib via `otelsql.Open("pgx", dsn)` (`go/README.md:359`),
`go-redis` v9 via `otelredis.Instrument` (`go/README.md:356`); Python
SQLAlchemy 2 + `psycopg` 3 via `instrument_sql`
(`python/staffops_otel_helper/ext/sql.py:80`), `redis-py` via
`instrument_redis` (`python/staffops_otel_helper/ext/redis.py:13`); Node.js
`pg` and node-redis via `sqlInstrumentation()` / `redisInstrumentation()`
(`nodejs/HOW-TO.md:610-645`; not ioredis, whose connect handshakes are root
traces, `nodejs/HOW-TO.md:673-678`); .NET Npgsql and StackExchange.Redis via
`AddOtelHelperSql()` / `AddOtelHelperRedis()` (`dotnet/README.md:584-618`).
Expected service-graph nodes (the connector makes a `database` edge named
after the first of `db.name` / `db.namespace` on a CLIENT span, else a
`virtual_node` edge named after the first peer attribute present —
`peer.service.name`, `peer.service`, `db.name`, `db.namespace`, `db.system`,
`db.system.name`; connector v0.165.0 `connector.go:50-57,270-298`):

| Edge | Go | Python | Node.js | .NET |
|---|---|---|---|---|
| backend → PostgreSQL (required `connection_type="database"`) | `virtual_node`, server `postgresql` — the helper passes no `db.namespace` (`go/ext/otelsql/sql.go:42-44`) → `xfail:A38` | `database`, server `demo_python` (`unmeasured`) | `database`, server `demo_node` (`db.namespace` written, `nodejs/HOW-TO.md:621`) | `database`, server `demo_dotnet` (`unmeasured`) |
| api → Redis, worker → Redis | server value recorded, not asserted, until DE6 | `virtual_node`, server `redis`: contrib redis v0.66b0 writes no `db.namespace` (`_semconv.py:605-613`, observability audit) | recorded | recorded (no namespace unless `OTEL_SEMCONV_STABILITY_OPT_IN` holds `database`, `dotnet/README.md:600-601`; the demo leaves it unset) |

A Redis client that writes the database index (`0`) as `db.namespace` would
turn its edge into `database` with server `0` and merge every language that
does so into one node; DE6 records which ones. Every XREADGROUP poll span
also makes a worker → Redis edge (DE-D5).
*(r2, 2026-11-16: trigger-raised duplicate and bind parameters (review H3);
digest pinning (LOW); platform-database sentence replaced (LOW).)*
*(r3, 2026-11-16: N1 — the r2 trigger raised "when the id exists" after a
seed insert, but the seed insert fires the same trigger; the trigger now
fires only on `dup-%` ids and raises unconditionally, no seed. N3/audit #15
— per-language node table; the r2 sentence guessing a Python Redis
namespace is replaced by the measured contrib behaviour.)*
*(r3.3, 2026-11-16, DE2 review: `demo/sql/schema.sql` starts with
`SELECT pg_advisory_xact_lock(4242);` and is sent without bind parameters as
one command — one implicit transaction under the simple query protocol (and
under Npgsql's single-Sync batch), so two backends starting at once apply it
in turn; drivers that split statements must keep the dollar-quoted function
body intact.)*
**Justification**: two pods instead of eight; a database per language keeps
the four languages' PostgreSQL nodes apart where the driver writes
`db.namespace`. The trigger puts the sentinel where an ADR-014 leak shows:
the driver's exception message. Bind parameters keep values out of
`db.query.text`.
**Trade-offs**: Redis nodes may merge across languages (above); shared
instances couple latencies (`unmeasured`).
**When it would be wrong**: the owner wants data to survive restarts; a
driver needs a server feature the pinned version lacks.
**Reversal action**: one PostgreSQL / Redis per language (DE-D12 re-run) or a PVC.
**Alternatives**: SQLite in-process (no network DB span); a seeded row with a
plain primary-key violation (the server's own message puts the value only in
`DETAIL`, which some drivers drop); an existing database elsewhere in the
cluster — not looked for: the run may write only in `dev-otel-libs` (DR11).

### DE-D5 — Redis Streams as the queue; one polling consumer per language
**Choice**: stream per language, consumer group `demo-workers`; the api
`XADD`s, the worker `XREADGROUP COUNT 10` **without `BLOCK`** every
`DEMO_POLL_SECONDS` (2 s) in every language and `XACK`s each message.
*(r2, 2026-11-16: the StackExchange.Redis evidence now cites the stream API
at the pinned version, review LOW.)*
**Justification**: no helper has a generic messaging extension; messaging
spans come only from the AWS SQS/SNS extensions (Go `go/README.md:366`; Python
`python/HOW-TO.md:494-502`; Node.js
`nodejs/packages/otel-helper-aws/src/internal/attributes.ts:143-146`). Every
language has a Redis extension (DE-D4). Streams have acks and a pending list
(`XPENDING`). No `BLOCK`: in StackExchange.Redis 2.6.122, the version the .NET
Redis tests use (`dotnet/README.md:587`), every `StreamReadGroup` overload
takes `position`, `count`/`countPerStream`, `noAck` and `flags`, and none a
block or timeout (`IDatabase.cs:2507,2521,2537,2554` at tag `2.6.122`); its
docs exclude blocking operations on the multiplexer ("the only redis
features that StackExchange.Redis does not offer (and will not ever offer)
are the 'blocking pops'",
[PipelinesMultiplexers.md at 2.6.122](https://github.com/StackExchange/StackExchange.Redis/blob/2.6.122/docs/PipelinesMultiplexers.md)).
*(r3, 2026-11-16: link pinned to the tag of the version in use, audit #22.)*
The same non-blocking poll everywhere keeps span shapes equal.
**Trade-offs**: each poll is a root Redis client span (≈ 30 per minute per
language; .NET parenting `unmeasured`); checks select by name and run.
**When it would be wrong**: a messaging extension joins the helpers, or root
poll traces flood Tempo (volume `unmeasured`).
**Reversal action**: move to the system the new extension covers, raise
`DEMO_POLL_SECONDS`, or wrap each poll in a worker span.
**Alternatives**: pub/sub (no ack); Kafka (no helper extension, new shared
infra); AWS extensions with LocalStack (AWS-shaped only).

### DE-D6 — Queue trace structure: CONSUMER span links to the producer and uses it as parent
**Choice**: the api starts a PRODUCER span `send demo:<lang>:orders` with
`messaging.system=redis`, `messaging.operation.name=send`,
`messaging.operation.type=send`, `messaging.destination.name=demo:<lang>:orders`
(the real stream key), `messaging.message.id` (the entry id `XADD` returns),
`server.address` / `server.port` (the Redis Service), and issues `XADD`
inside it with the fields `traceparent`, `tracestate`, `baggage` written by
the global propagator's `inject` into a map carrier. Per message the worker
`extract`s the map and starts a CONSUMER span `process demo:<lang>:orders`
(`messaging.operation.name=process`, `messaging.operation.type=process`,
same destination, message id, `messaging.consumer.group.name=demo-workers`,
`server.address` / `server.port`) with **one link** to the extracted context
**and** that context as parent, outside any other active span, then `XACK`s
inside it.
*(Corrected 2026-11-16, r2, review H1: r1 said the service-graph connector
pairs spans only by parent-child and "has no rule for links", so a
link-only consumer would give no edge. Wrong: in connector v0.165.0 a
CONSUMER span with links creates one edge per link keyed by
`NewLinkedConsumerKey` and reconciled with the producer's key, and the
parent is used only when the span has no link (`connector.go:300-358`,
`internal/store/store.go:167-214`). The edge comes from the link; the
parent is kept for the single-trace view. Attributes
`messaging.consumer.group.name`, `server.*` and the real stream key as the
destination added, review M8.)*
**Justification**: semconv v1.43.0 messaging: "For each message it accounts
for, the 'Process' or 'Receive' span SHOULD link to the message's creation
context"; "Exclusively for single messages scenarios, the 'Process' span MAY
use the message's creation context as its parent"; it is "NOT RECOMMENDED"
as parent "if processing happens in the scope of another span"
([messaging-spans.md v1.43.0](https://github.com/open-telemetry/semantic-conventions/blob/v1.43.0/docs/messaging/messaging-spans.md)).
The demo processes one message per span outside other spans, so the parent
is allowed; it keeps worker → api → backend and api → queue → worker in one
Tempo trace, which check e and the owner's "full trace" ask for. The link is
the SHOULD and is what the connector pairs on. `messaging.system=redis` is a
custom value (no Redis entry in the v1.43.0 well-known list); the messaging
conventions are `Development` status.
**Trade-offs**: the producer's trace includes the asynchronous processing and
its queue lag (≤ one poll interval). The connector store keeps an unpaired
span for `ttl: 10s` (gateway CR `connectors.service_graph.store.ttl`, read
2026-11-16) while producer and consumer travel through independent SDK and
Collector batches (agent and gateway `batch.timeout: 5s`, same CRs), so some
messaging edges may expire; the share is `unmeasured`, so check e requires
≥ 0.8 × the expected count and cross-checks the links in Tempo (DE-D11).
*(r3, 2026-11-16: `expired_edges` no longer used — cluster-wide and
routinely non-zero, audit #9.)*
**When it would be wrong**: the consumer processes batches per span (then
link only, per semconv), or the expired-edge share makes the edge check flaky.
**Reversal action**: drop the parent (link only — the edge still forms
through the link; check e then asserts the link only and the queue path
becomes a second trace); for expiries, ask the platform owner for a longer
`store.ttl` (a request, DE-D14) or check the link in Tempo instead of the edge.
**Alternatives**: link only (two traces per order, the owner's single view
lost); parent only (violates the semconv SHOULD; the connector would fall
back to parent pairing).

### DE-D7 — Deterministic scenarios and load
**Choice**: the worker sends one `POST /orders` every `DEMO_INTERVAL_SECONDS`
(5 s), cycling a fixed sequence of 10: `ok` at 1, 2, 4, 5, 7, 8, 10;
`business_error` at 3; `db_error` at 6; `unhandled` at 9 — one of each error
per 50 s. Each request runs in a new root span `demo.drive` (helpers'
`StartRootSpan` `go/tracing.go:142`, `start_root_span`
`python/staffops_otel_helper/tracing.py:220`, `startRootSpan`
`nodejs/example/node-custom-signals/custom-signals.js:27`; .NET an
`ActivitySource` root with `parentContext: default`) with `demo.scenario`.
Body `{"order_id", "customer", "amount", "scenario"}`; `customer` cycles over
three values so the cache hits and misses.
*(r3.4, 2026-11-16, after DE3a–d: conventions the spec left open, pinned so
the four languages match. **Run id**: apps read `otel_libs.run` from
`OTEL_RESOURCE_ATTRIBUTES`, fallback `local`; no separate variable.
**Order ids**: `ok-<seq mod 500>` for `ok`, `ord-<seq mod 500>` for the error
scenarios; the backend inserts `dup-<run>` itself for `db_error`, so
`order.id` never carries a run token (DR12). **Amounts**: `business_error`
sends 5000 and the backend rejects any amount over 1000; the other scenarios
send ≤ 1000. **Wiring**: `DEMO_API_URL` is a base URL (the worker appends
`/orders`); the backend reads the schema from `DEMO_SCHEMA_FILE`, default
`/etc/otel-libs-demo/schema.sql`, baked into the image (Go cannot embed a
file outside its module; the others may embed it but honor the variable).
**Health**: every backend serves `grpc.health.v1`; every api serves
`GET /health`. **Accept span on 4xx**: `demo.order.accept` is ERROR with no
description, no `error.type`, no event and no log (only the owning span names
the failure, DE-D8). *(Corrected 2026-11-17, DE-D8 A45 note: on 422 / 409 /
502 it carries `error.type` = the HTTP status it answers, as `demo.drive`
does; still no description, no event, no record.)* **Panic/exception at span end**: an SDK that records an
exception when a span ends during unwinding (Go `span.End()` deferred in a
panic adds `exception.message`) must not see it — end the span before
re-raising.)*
**Justification**: a 15-minute window holds 180 requests per language: 126
`ok` and 18 of each error scenario, minus at most one cycle at the edges —
the floor for the DE-D11 minimums.
**Trade-offs**: 12 requests per minute is far from production; the demo
validates correlation, not performance.
**When it would be wrong**: a check needs more samples than a window holds.
**Reversal action**: shorten `DEMO_INTERVAL_SECONDS` (dated note; DE-D12 re-run).
**Alternatives**: random error share (non-deterministic counts); k6 (one
more image; the worker must exist for the queue anyway).

> *Amended 2026-11-19 (soak r6, owner ruling 2; task DE13; not yet reviewed):*
> **env-override hook.** The soak (`specs/soak-simulation/` r6) reuses the
> demo as its workload: k6 drives `POST /orders`, the worker keeps consuming
> the queue with `DEMO_INTERVAL_SECONDS=3600`, and every app exports to the
> soak's Collector copy. `demo.sh apply --lang|--all` therefore gains an
> optional env file `DEMO_ENV_FILE`: one `<lang>/<app> NAME=VALUE` per line,
> `*` allowed for either part, `#` comments. Rules: (1) a line **replaces**
> the value of a variable `app_env` already sets, or adds a new one — never a
> second entry with the same name (server-side apply rejects duplicate keys
> in the `env` list); (2) only names matching `OTEL_*` or `DEMO_*` are
> accepted, and never `DEMO_*_PASSWORD_FILE`, `DEMO_PG_*` or `DEMO_REDIS_*`
> (the DE-D13 credential wiring is not overridable); any other line fails
> `apply` before a write; (3) `render` prints the overridden values, with
> the value of any `*_HEADERS` variable shown as `<set>`; (4) the K-D2
> pre-flight and `FailedScheduling` watch run unchanged — a changed value
> changes the pod template, so `apply` rolls the Deployment (`Recreate`);
> (5) without `DEMO_ENV_FILE` the rendered manifests are byte-identical to
> today's. The demo's own checks (`verify.py demo`) assume the default 5 s
> interval; a run with the hook's 3600 s worker is not a DE-D11 run.
> **Trade-off**: an env file is one more input to keep out of git when it
> carries the soak's fake canary token (it lives in the soak's git-ignored
> run directory). **Reversal action**: remove `DEMO_ENV_FILE` handling; the
> soak then renders its own manifests (a second copy of the app manifests).

> *Amended 2026-11-20 (soak r6.1, owner ruling on AG8; task DE14; not yet
> reviewed):* the soak's loaded window sets `DEMO_INTERVAL_SECONDS=10800`
> (not 3600, above). For the soak's multi-worker check (K8c) the Python api
> gains an opt-in server mode: `DEMO_PY_SERVER=gunicorn` with
> `DEMO_PY_WORKERS=<N>` runs gunicorn with `uvicorn.workers.UvicornWorker`,
> `--preload`, and the helper set up in `post_fork` (the D4 fork path; the
> plain `uvicorn --workers` path spawns and skips it); unset keeps today's
> single `uvicorn.run`. gunicorn becomes a new pinned dependency in
> `python/demo/requirements.lock` — a dependency addition the owner approves
> before DE14 starts (licence: MIT, to be confirmed against the pinned
> release). **Reversal action**: drop the mode and the dependency; K8c then
> needs a soak-owned Python image.

> *Amended 2026-11-23 (soak r6.2 "lean soak", owner ruling; tasks DE13, DE14;
> not yet reviewed):* (1) **DE14 is dropped** — the soak accepts multi-worker
> Python as a gap (soak AG8), so the gunicorn mode of the 2026-11-20
> amendment above is not built and **no dependency is added**; that
> amendment stays as dated history. (2) The DE13 hook gains three rules:
> `DEMO_RESTART_NONCE` is an allowed `DEMO_*` name the apps ignore — a new
> value changes the pod template, so `apply` rolls the Deployment (the
> soak's forced restart); `demo.sh scale --lang|--all <n>` scales the
> selected demo Deployments to `n` (the backing services are never scaled)
> and runs no pre-flight for `n = 0`; and the allow-list **denies**
> `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` (with the credential
> names already denied), so the soak cannot change the identity its K2, K3
> and K5 checks read. **Reversal action**: remove the nonce, the `scale`
> subcommand and the two denials; the soak then restarts pods with
> `kubectl rollout restart` and scales with `kubectl scale` (two more
> named writes in its scope rule).

> *Amended 2026-11-23 (DE13 and soak K-hang implementation; owner accepted
> (1) on 2026-11-23; (2) records calls the amendments above left open):*
> (1) **`--force-conflicts` on the apps apply.** `demo.sh scale` uses
> `kubectl scale`, an Update through the `scale` subresource; Kubernetes
> server-side apply then moves ownership of `.spec.replicas` from the
> `otel-libs-demo` field manager to `kubectl`. A later `demo.sh apply` would
> set `replicas: 1` on a field another manager owns, and when the value
> differs (after a scale to 0, the soak's case) server-side apply refuses it
> with a conflict. The apps apply therefore passes `--force-conflicts` and
> takes the field back, resetting the replicas to 1. Only the demo app
> manifests are applied this way; the backing services' apply is unchanged.
> *Trade-off*: a manual edit of a demo app field by another manager is
> overwritten silently — intended, the script owns these objects.
> *Not measured on k8s01* (the conflict follows the documented ownership
> rules). *Reversal*: drop the flag and make `scale` render and apply the
> Deployments itself (then it must also get the soak's `DEMO_ENV_FILE`, or
> it changes the pod template). (2) **Decisions** (DE13): `scale` refuses
> when one of the selected apps' Deployments is not applied (nothing is
> scaled); for `n` > 0 it runs the K-D2 pre-flight on n times the requests
> **and** the FailedScheduling watch of `apply`; `n` is 0-999 without a
> leading zero; `scale --all` follows `LANGS`, as `apply --all` does;
> `delete` and `scale` ignore `DEMO_ENV_FILE`. (K-hang, `testing/fakeotlp`):
> in `--hang` mode every OTLP request hangs, so the redirect paths and the
> echo and gRPC-redirect headers are not honored (a sink is one mode at a
> time; the redirect mode runs the default receiver); at most 256 calls are
> held (`--hang-max` / `HANG_MAX`), the oldest closed beyond the cap; the
> admin listener adds `GET /held` (`{"held": n, "evicted": m}`) so a run can
> see that calls are held.

### DE-D8 — Exceptions in demo code follow ADR-014 by hand; instrumentations are left as they are
**Choice**: demo spans use the tracer name `otel-libs-demo` (the scope check d
selects them by).
- **Owning span.** A failure is handled in its owning demo span (Scenario
  table) by one pattern: catch → status ERROR (no description), `error.type`
  = the pinned type literal, an `exception` event with only `exception.type`
  → one ERROR log record **while that span is current** (body = message,
  attributes `exception.type`, `exception.message`, `exception.stacktrace`)
  → rethrow (or map to the gRPC status). For `unhandled` this happens inside
  `demo.order.accept` in the api; the framework's (Go: the api's recovery
  middleware's) top-level handler only maps the escaped exception to `500`
  and logs nothing of its own on that path. Go uses a deferred function in
  `demo.order.accept`: `recover()` → record → log → `panic(r)`.
- **Pinned literals.** Types: `OrderRejectedError`, `OrderExistsError`,
  `DemoUnhandledError` (unqualified, the same in every language; Go maps its
  `*main.X` types with an `errorType` helper as
  `go/example/go-custom-signals/main.go:216-222`; .NET writes the short name;
  Node.js classes set `this.name`, as
  `nodejs/example/node-custom-signals/custom-signals.js:43-52`). This is a
  deliberate deviation from the semconv recommendation that `error.type` be
  the exception's canonical, fully qualified type name
  ([error.type, semconv v1.43.0](https://github.com/open-telemetry/semantic-conventions/blob/v1.43.0/docs/registry/attributes/error.md)),
  chosen so one query matches every language. Messages carry run tokens that
  differ from the gRPC status messages: `OrderRejectedError("amount over
  limit rej-<run>")`, `OrderExistsError("order dup-<run> exists")` (wrapping
  the driver error whose message is `duplicate order dup-<run>`, DE-D4),
  `DemoUnhandledError("boom unh-<run>")`; gRPC status messages stay
  constant: `order rejected`, `order exists`. The checks scan for the tokens
  `dup-<run>`, `rej-<run>`, `unh-<run>` only, so a constant gRPC message in a
  client span's status description (semconv-compliant, see d.grpc) is not a
  hit.
- **Log record's `exception.type`.** Written by each language's logging
  bridge, not by demo code, except in Go (demo code sets it). It is required
  non-empty and recorded, compared with the pinned literal only for Go.
  Measured for .NET (Loki, 2026-11-16, example records): `exception_type` =
  the short name (`InvalidOperationException`, `RpcException`,
  `TaskCanceledException`), `severity_text` `Error`, `severity_number` `17`.
- **Parent demo spans** that only propagate the failure set ERROR without an
  event or log. `demo.drive` (worker): a non-2xx answer → ERROR,
  `error.type` = the status code, `demo.outcome=error`, no event, one WARN
  `order request failed`.
- **Per-language calls.** Go: `span.AddEvent("exception",
  trace.WithAttributes(semconv.ExceptionType(…)))`, never `span.RecordError`
  (`go/example/go-custom-signals/main.go:193`); log via
  `slog.New(otelhelper.NewSlogHandler())` (`go/logging.go:490`)
  `ErrorContext`; `exception.stacktrace` = `debug.Stack()` at the log site —
  the **logging goroutine's stack** (ruling DE-O7); in the panic path the
  deferred recover runs on the panicking goroutine, so it is that stack.
  Python: `start_as_current_span(…, record_exception=False,
  set_status_on_exception=False)`, `span.add_event(…)`, `logger.error(msg,
  exc_info=e)` (`custom_signals.py:141-143`); the api's gRPC client is
  **`grpc.aio`** (as `python/example/python-api/main.py:96`), so the
  contrib file that governs its client span is the aio client interceptor
  (`instrumentation/grpc/_aio_client.py` at contrib 0.66b2 — behaviour
  `unmeasured`); the backend (a `grpc.aio` server, as
  `python/example/python-backend/server.py:145`) fails a call with `await
  context.abort(code, details)`. That still records an exception on the
  server span: grpc v1.75.0 `aio/server.pyx.pxi:174,201` raises
  `AbortError('Locally aborted.')` (`AbortError(BaseError)`,
  `BaseError(Exception)`, `common.pyx.pxi:85,98`), and contrib 0.66b2
  `_aio_server.py:107-114` records any error whose type is not exactly
  `Exception`; the aio client interceptor (`_aio_client.py:95-115`) sets
  ERROR with description = details and records no exception on a unary
  status. The server span's description is `"{code}:{details}"` only for
  the six server-error codes (`_utilities.py:29-40`). (`_server.py:283-291`
  is the sync server, cited in r3 by mistake.)
  *(r3.1, 2026-11-16: M1 — r3 said `context.abort` avoids the exception
  record; it does not.)* Node.js:
  `span.addEvent('exception', …)`, never `span.recordException`
  (`custom-signals.js:125`); `logger.error(msg, { error: err })`
  (`custom-signals.js:130-133`); gRPC handlers return errors through the
  callback (root `README.md:313`). .NET: the custom-signals pattern
  (`dotnet/example/dotnet-custom-signals/Program.cs:184-197`),
  `ILogger.LogError(ex, …)` inside the activity scope.
- Instrumentation spans are not altered; they are measured per span set
  (DE-D11 check d) as strict `xfail` cells (ruling DE-O6).
*(r2, 2026-11-16: pinned type literals, scope `otel-libs-demo`, owning span,
`demo.drive` rule (review M4, M5, H4); rulings DE-O6, DE-O7.)*
*(r3, 2026-11-16: N2 — the log is written inside the owning span before the
rethrow, not by the top-level handler (r2 left the record outside the span
for `unhandled`); audit #7 — run-token messages distinct from the gRPC
messages; audit #18 — semconv deviation stated, log `exception.type`
measured for .NET; N8/audit #14 — Python gRPC client and server calls
pinned.)*
*(Corrected 2026-11-17, A45 migration — task DE12a, split from DE12: the demo apps in
Go, Python and Node.js now use the helper's application-span API
([ADR-016](../decisions/ADR-016-application-span-failure-status.md), contract
design D15) instead of their hand-written failure code, which is removed
(Go `demokit.RecordFailure`, Python `democommon.record_failure`, Node.js
`ownFailure` / `recordFailure` / `logFailure`). The "Owning span" pattern and
the "Per-language calls" above are replaced as follows; .NET keeps its
hand-written `Failure.Record` (its library has no A45 API, parked) and the
.NET library is unchanged.*
- *No `exception` event in any language. A45 writes none, and the .NET
  demo's `Failure.Record` no longer adds its type-only event, so the owning
  span carries status ERROR without description and `error.type` only; the
  detail is in the one correlated record. Instrumentation spans keep their
  own events (d.db is unchanged, A36).*
- *Pinned literals through the per-error override of D15: Go an
  `ErrorType() string` method on `OrderRejectedError`, `OrderExistsError`,
  `DemoUnhandledError` (found before the wrapped driver error); Python the
  class attribute `__otel_error_type__`; Node.js a prototype getter under
  `Symbol.for('otel.error_type')`; .NET the short type name as before. The
  metric attribute `error.type` (DE-D9) comes from a demo function that
  mirrors the helper's rule (override, else the helper's default), so the
  metric value equals the span value also for an unexpected failure.*
- *Owning spans run in the span function: Go `otelhelper.WithSpan`, Python
  `staffops_otel_helper.traced`, Node.js `withSpan` — backend
  `demo.order.process` (the failure is returned or thrown out of it, then
  mapped to the gRPC status after the span ended) and api
  `demo.order.accept` (`unhandled` leaves it as a panic or exception; the
  helper marks the span, writes the record, ends the span and re-raises for
  the framework's 500). The custom metrics are still recorded inside the
  span.*
- *Propagating spans are not marked by the helper: they keep a hand marking
  with the helper's shape (status ERROR without description, `error.type`,
  no event) and write no record — api `demo.order.accept` on 422 / 409 / 502:
  `error.type` = the HTTP status it answers (`422`, `409`, `502`; until this
  note it had no `error.type`), all four languages; worker `demo.drive`:
  `error.type` = the status code, or the root cause's type name on a
  transport failure, plus one WARN. The failure never leaves the span function
  there — it is answered or caught inside — so the helper adds no record.
  `demo.drive` stays on each language's root-span starter: Go `StartRootSpan`
  is a plain span starter (no failure rule); Python `start_root_span` (now
  without `record_exception` / `set_status_on_exception`, which A45 rejects
  with `TypeError`) and Node.js `startRootSpan` apply the A45 rule, but it
  never fires because every failure is caught inside.*
- *Record count: **one ERROR record per failure, at the owning span**, as
  before. Why this mapping and not "every demo span records": D15 writes one
  record per helper span a failure crosses and advises using the span
  function only at the layers that should report; semconv recording-errors
  says a propagated exception SHOULD be recorded once; the common practice
  is the same (Java `@WithSpan` and the Python SDK mark each span an
  exception leaves; the application logs the exception once, where it is
  handled).
  Letting the failure cross the propagating spans as an exception would add
  one record per layer — for `business_error` and `db_error` a second on
  `demo.order.accept` and a third on `demo.drive` — each with no new
  information, and each in another service's logs.*
- *Record source: Go, Python and Node.js records now come from the helper's
  failure channel (Go: the helper's slog handler, logger `otel-helper` on
  stdout; Python: logger `staffops_otel_helper.failure`; Node.js: the helper's
  `write()` path, scope `otel-helper`), body = message, `exception.type` = the
  pinned literal. The Node.js record no longer carries `demo.scenario`; the Go
  stack is `debug.Stack()` at `WithSpan`'s marking (for a returned error it
  shows the `WithSpan` frames, D15; for a panic it is taken at the recover and
  shows the panic site). *Updated 2026-11-17 (A45 follow-up):* the helper's
  leading frames are now skipped, so the first frame is the caller of
  `WithSpan` (D15). The .NET `Failure.Record` calls of the owning spans
  no longer pass `demo.scenario` / `order.id`, so in all four languages the
  record carries only the `exception.*` attributes.*
- *Node.js: `api/app.js` and `backend/app.js` stop at load when the helper
  has no `withSpan` (an image not built from the workspace); the backend's
  PlaceOrder body is `lib/place-order.js` `placeOrderInSpan`, unit-tested with
  a fake `withSpan`; `lib/failure.js` `errorTypeOf` follows the helper's rule
  (instance and `static` override, `AbortError` caused by a `TimeoutError` →
  `TimeoutError`, an override on a non-Error, a throwing getter falls through).*
- *Check d.demo (DE-D11): per inspected error trace, the owning span has
  status ERROR, an **empty** description, the pinned `error.type` and **no**
  `exception` event; the trace holds **exactly one** ERROR record
  (`severity_number ≥ 17`) on or below the demo spans of the language's three
  services, and it sits on the owning span, with `exception_type` = the pinned
  literal in all four languages (.NET: the measured short name). Not counted:
  the helper record of a failed PostgreSQL span (d.db's) and records on
  framework spans outside every demo span (the .NET Kestrel record on the api
  SERVER span for `unhandled`, design r3.5 "Framework logs on the unhandled
  path", A37). The propagating spans are checked too: api `demo.order.accept`
  (`business_error`, `db_error`) and worker `demo.drive` have status ERROR,
  no description, `error.type` = `HTTP_CODES[scenario]` (`422` / `409` /
  `500`), no `exception` event and no ERROR record. Check c requires the
  metric `error_type` of the `business_error` / `db_error` series to equal
  the pinned literal (= the span value).*
- *The recorded fixtures of `testing/k8s/demo/fixtures/` were edited by hand
  for this note (owning-span events removed, `error.type` added on the 4xx
  `demo.order.accept` spans); they are re-recorded from the cluster after the
  next run. The examples replacement (the rest of DE12) is still open.*
**Justification**: ADR-014 decision 4 applies to new code; instrumentation
gaps map to open tasks (A36, `specs/config-contract-v0.3/tasks.md:58`; A37,
`tasks.md:59`; inventory root `README.md:367`). A record written while the
owning span is current carries that span's ids in every language's bridge,
which check d needs.
**Trade-offs**: the run is green only with `xfail` cells naming the debt;
unqualified type names lose package information.
**When it would be wrong**: the owner wants the target state shown now, or
the fully qualified name per semconv.
**Reversal action**: per-language suppression behind
`DEMO_SUPPRESS_FRAMEWORK_EXCEPTIONS=true` (.NET `RecordException = false`,
`dotnet/HOW-TO.md:815`); for names, switch the literals to each language's
qualified name and make check d compare per language.
**Alternatives**: Collector scrub (platform config); logging in the
top-level handler (outside the owning span — r2's gap).

### DE-D9 — Custom metric names, units, buckets and attributes
**Choice**: in `demo.order.process`: counter `demo.orders.processed` (unit
`{order}`, integer in every language — Node.js `valueType: ValueType.INT`,
as `nodejs/example/node-process/workers.js:20-22`), histogram
`demo.order.duration` (unit `s`, boundaries `0.005, 0.01, 0.025, 0.05, 0.1,
0.25, 0.5, 1, 2.5` through each SDK's advisory API — Go
`metric.WithExplicitBucketBoundaries` (`go/example/go-custom-signals/main.go:145`),
Python `explicit_bucket_boundaries_advisory`, Node.js
`advice.explicitBucketBoundaries`, .NET `InstrumentAdvice<double>`);
attributes `demo.scenario`, `demo.outcome`, `error.type` (failure only, the
DE-D8 literals). Expected series (`kps` `spec.otlp.translationStrategy:
UnderscoreEscapingWithSuffixes`, read 2026-11-16):
`demo_orders_processed_total`, `demo_order_duration_seconds_bucket|_sum|_count`,
labels `demo_scenario`, `demo_outcome`, `error_type`, `job=dev-otel-libs/demo-<lang>-backend`,
`k8s_pod_name` (promoted, soak K-D9).
**Justification**: the `order.*` names of the custom-signals examples
(`go/example/go-custom-signals/main.go:134-157`,
`dotnet/example/dotnet-custom-signals/Program.cs:99-118`) would collide with
the examples' series; seconds avoid the SDK millisecond defaults
(`main.go:140-141`); recording inside the span attaches `trace_based` exemplars.
**Trade-offs**: none new.
*(Note 2026-11-17, A45 migration: a cancellation leaves the helper's span
Unset with no record (contract design D15), but the demo's metric code still
counts it as `demo.outcome=error` with its `error.type`, because it runs
inside the span on any exception. No demo scenario reaches that path (the
backend is never cancelled by the cycle), so span and metric cannot disagree
in a run; documented, not handled.)*
**When it would be wrong**: the translation strategy changes.
**Reversal action**: derive the expected names in `verify.py demo` from the
pre-flight's read of `spec.otlp`.
**Alternatives**: reuse `order.*` (collisions); milliseconds (non-semconv unit).

### DE-D10 — Exemplars: SDK `trace_based`, cluster storage enabled, checked on the cluster only
**Choice**: the apps leave `OTEL_METRICS_EXEMPLAR_FILTER` unset. Check b
queries `/api/v1/query_exemplars` on the series that carry exemplars — the
histogram **`_bucket`** series and counter `_total` series, never the base
name (audit, 2026-11-16: the base name returned 0 exemplars, `_bucket` 51):
`http_server_request_duration_seconds_bucket`,
`rpc_server_call_duration_seconds_bucket`,
`db_client_operation_duration_seconds_bucket`,
`demo_order_duration_seconds_bucket`, and optionally
`demo_orders_processed_total`, per (language, metric) of the Metric map,
scoped to the run by `instance` values read from
`target_info{otel_libs_run="<run>"}` over the window. Up to 20 distinct trace
ids per cell are resolved in Tempo, and each exemplar's `span_id` must be a
span of its trace. Pass = ≥ 1 exemplar and every tried id and span resolves.
Node.js cells are `n/a:upstream-no-sdk-exemplars` (root `README.md:329`;
`@opentelemetry/sdk-metrics` 3.0.0 has no exemplar code, audit), strict like
every `n/a` (DR9). The run report records
`prometheus_tsdb_exemplar_exemplars_in_storage` against the configured
`maxSize` 100000 (a full buffer evicts old exemplars). The gateway
span-metrics exemplars are an informational line. The pre-flight reads
`/api/v1/status/flags` and the Prometheus start time: `exemplar-storage`
absent → BLOCKED (fails, DE-D11); a start time inside the window → the report
states exemplars before it were lost (in-memory storage).
Diagnostic only, `unmeasured`, never a gate: the helper's Prometheus
`/metrics` listener read from inside the namespace with `Accept:
application/openmetrics-text`, to tell an SDK-side gap from a pipeline gap.
*(r2, 2026-11-16: ruling DE-O2 — exemplar storage enabled (read-only check
≈ 14:20 UTC: `spec.enableFeatures: ["exemplar-storage"]`,
`spec.exemplars.maxSize: 100000`, flag `enable-feature=exemplar-storage`,
Prometheus start 2026-11-16T14:14:31Z); r1's `blocked: platform` default
and the Compose fallback removed; Node.js `n/a` (review M2); A33 no longer a
gap owner (Python's `TraceBasedExemplarFilter()`,
`python/staffops_otel_helper/metrics.py:498`, equals the default).)*
*(r3, 2026-11-16: audit #5 — selectors moved to `_bucket` / `_total`, run
scoping by `target_info`, span-id check, storage gauge; OTLP exemplars end
to end are now **measured for Go**: on 2026-11-16 the live Go examples had
51, 36 and 37 exemplars on the server, backend and client duration
buckets, 22 and 22 on two custom histograms, and 8 of 8 sampled trace ids
resolved in Tempo (observability audit, 14:23–14:42 UTC). Python and .NET:
`unmeasured` (no live app).)*
**Justification**: Prometheus keeps exemplars only with `exemplar-storage`
([Prometheus feature flags](https://prometheus.io/docs/prometheus/latest/feature_flags/#exemplars-storage));
Go (SDK), .NET (helper sets the filter, `dotnet/HOW-TO.md:613`) and Python
(`metrics.py:498`) attach `trace_based` exemplars; the Go measurement shows
the OTLP receiver path keeps them.
**Trade-offs**: a Prometheus restart during a run empties exemplars; the
report says so rather than failing silently.
**When it would be wrong**: the platform owner turns the flag off.
**Reversal action**: b goes BLOCKED (fails); ask the owner for a ruling.
**Alternatives**: a namespaced Prometheus and Collector (pipeline copy,
soak K-D1); a local Compose Prometheus (removed by the ruling).

### DE-D11 — `verify.py demo`: span sets, minimums, settle delay, hygiene set
**Choice**: mode `verify.py demo --run <run> [--langs …] [--window 15m]
[--settle 2m]`; window = `[now − settle − W, now − settle]`. Settle 2 min is
the **minimum**: the `service_graph` connector flushes every 60 s (no
`metrics_flush_interval` in the gateway CR, default 60 s, connector v0.165.0
`connector.go:59`), after its 10 s store TTL and the 5 s agent and gateway
batches, so the first service-graph series appear 60–90 s after traffic. Run
scoping uses the resource attribute `otel_libs.run`: Tempo
`resource.otel_libs.run`, Loki `otel_libs_run`, Prometheus a join on
`target_info{otel_libs_run="<run>"}` by `job`, `instance` (not a pod-name
regex). It reuses `Backends._get` (`testing/k8s/verify.py:66-79`) and adds a
Prometheus `query_range` client and a `kubectl get -o json` wrapper (shared
with soak K1, K-D3); it reads the run's pods by label `otel-libs/run=<run>`
for the hygiene scope. Each line is PASS / FAIL / XFAIL / XPASS / NA /
UNEXPECTED / BLOCKED; exit 1 on FAIL, XPASS, UNEXPECTED or BLOCKED. Expected
cells: `testing/k8s/demo/expected.json`; `tools/spec_lint.py`'s `xfail-task`
rule is extended to it (DE5). Ids are normalized per "Id encodings" before
any comparison. Severity is compared by number: ERROR = `severity_number ≥
17` (.NET writes `severity_text` `Error`, the others `ERROR`); the sampled
flag is `flags & 1` (never `flags="1"`: Python records carry `3`, audit).
- **a (logs → traces)**: OTLP streams only —
  `{service_name=~"demo-<lang>-(worker|api|backend)", service_instance_id!=""} | otel_libs_run="<run>" | trace_id!=""`
  (the `otel-logs` `file_log` streams carry `service_name` = the container
  name — e.g. `go-api`, `go-backend` — and no `service_instance_id`, soak
  K-D3; *r3.1, 2026-11-16: r3 said the Deployment name*). Minimum:
  `sum by (service_name)(count_over_time(<selector>[W]))` ≥ 1 for each of the
  three services and the total ≥ half the cycle's expected records (two INFO
  records per `ok` order plus the error records ≈ 360 per 15 min per
  language, DE-D7) — 0 is FAIL. Trace ids are sampled across the whole
  window by time slices (the existing `loki_lines` limit is 200 lines,
  `verify.py:95-110`), up to 200 per language; each must resolve and the
  record's `span_id` must be a span of that trace.
- **b (exemplars)**: DE-D10.
- **c (custom metrics)**: per language,
  `increase(demo_orders_processed_total[W]) and on (job, instance) last_over_time(target_info{otel_libs_run="<run>"}[W])`
  > 0 for each of `demo_scenario` = `ok`, `business_error`, `db_error`, and
  the same on `demo_order_duration_seconds_count`; a backend series with
  `demo_scenario="unhandled"` is UNEXPECTED (the scenario never reaches the
  backend). *(r3.1, 2026-11-16: H1 — r3 required all four scenarios, which
  could never pass.)* `increase()` misses a new
  series' first sample, so the check also accepts a series whose first sample
  lies inside the window with a value > 0. Structure, not
  `/api/v1/metadata` (it returns `{}` for OTLP-only metrics, audit): the
  counter has `_total` and no `_bucket`; the histogram has `_bucket` with
  `le="+Inf"`, `_sum` and `_count`; `le` values compared as floats against
  the DE-D9 bounds; labels ⊇ {`demo_scenario`, `demo_outcome`}, `error_type`
  on and only on `demo_outcome="error"`, and on the `business_error` /
  `db_error` series equal to the pinned literal (= the owning span's
  `error.type`; *added 2026-11-17, DE-D8 A45 note*).
- **d (error routes)**: per error scenario, TraceQL
  `{resource.service.name="demo-<lang>-worker" && resource.otel_libs.run="<run>" && span.demo.scenario="<s>"}`;
  ≥ 1 trace per scenario per language, up to 5 inspected. Per span set, each
  with its own cell:
  - **d.demo** — scope `otel-libs-demo`: the owning span has ERROR, an
    empty description, the pinned `error.type`, no `exception` event;
    Loki `{service_name=~"demo-<lang>-(worker|api|backend)", service_instance_id!=""} | trace_id="<id>"`
    holds exactly one record with `severity_number ≥ 17` on or below the
    demo spans (PostgreSQL-span and framework-span records not counted), on
    the owning span, with non-empty `exception_stacktrace` and
    `exception_type` = the pinned literal; the propagating spans (api
    `demo.order.accept` on 422 / 409, worker `demo.drive`) have ERROR, an
    empty description, `error.type` = the HTTP status, no event, no record.
    (*Corrected 2026-11-17, A45 migration, DE-D8 note: was one event with only
    `exception.type` and one record at the owning span, `exception_type`
    compared for Go only.*)
  - **d.grpc** — client (api) and server (backend) spans. Client: ERROR
    for every non-OK code; a description equal to the constant gRPC message
    (`order rejected` / `order exists`) is allowed — semconv-compliant, and
    Go's otelgrpc v0.72.0 client writes the status message as the
    description for every non-OK code (`stats_handler.go:304-306,386-389`,
    audit). Server: UNSET for `FAILED_PRECONDITION` / `ALREADY_EXISTS`. No
    `exception.message`, `exception.stacktrace`, `grpc.error_message`.
  - **d.http** — `unhandled`: api SERVER span ERROR with `error.type=500`;
    no `exception.message` / `exception.stacktrace`. For the 4xx scenarios
    the api SERVER span is UNSET and the worker's HTTP CLIENT span is ERROR
    with `error.type` `422` / `409` (HTTP semconv), checked as part of d.http.
  - **d.db** — CLIENT spans with `db.system.name="postgresql"`
    (`db_error`): ERROR, empty status description (contract A25),
    `db.response.status_code="23505"`, an event with only `exception.type`,
    and the helper's correlated record (`severity_number ≥ 17`, ADR-014).
  On every span of the trace, the status description, attributes and event
  attributes are scanned for the run tokens `dup-<run>`, `rej-<run>`,
  `unh-<run>` (DE-D8); a hit fails the set the span belongs to.
- **e (structure)**: ≥ 1 `ok` trace whose root started before the window end,
  matched against "Expected trace" by kind, name pattern and key attributes
  (framework spans allowed, `testing/k8s/README.md:207-208`); DB spans by
  kind CLIENT + `db.system.name`. The CONSUMER span's link and parent both
  equal the PRODUCER span id. Service graph, summed over `failed` (never
  filtered on `failed="false"`):
  `sum by (client, server, connection_type)(increase(traces_service_graph_request_total{client=…,server=…}[W]))`.
  HTTP and gRPC edges have **no** `connection_type` label (empty value =
  absent): selected with `connection_type=""`; worker → api and api →
  backend > 0, with the failed share recorded (expected ≈ 0.3 for worker →
  api: 3 of 10 answers are 4xx/5xx; ≈ 0.22 for api → backend: 2 of the 9 calls
  that reach the backend fail; measured on the Go examples: api → backend
  96.4 requests / 15 min with no `connection_type` label, an external
  `user → api` edge 144.6 with `virtual_node`, audit). Messaging edge api →
  worker: `connection_type="messaging_system"`, count ≥ 0.8 × the expected
  `ok` orders in the window, cross-checked by the TraceQL count
  `{resource.otel_libs.run="<run>" && kind=consumer && link:spanID != nil}`.
  PostgreSQL edge: `connection_type="database"` (DE-D4 table; Go
  `xfail:A38`). `otelcol_connector_servicegraph_dropped_spans` increase is
  reported; `otelcol_connector_servicegraph_expired_edges` is **not** used
  (cluster-wide, routine virtual-node expiries: 129 of 220 edges, audit).
- **g (hygiene)** — **token pattern** (anchored, both scans):
  `(^|[^0-9a-f])[0-9a-f]{48}([^0-9a-f]|$)`; plus `(?i)password=`,
  `://[^/@]+:[^@]+@` and the run tokens. **Span set of the run**: Tempo
  `{resource.otel_libs.run="<run>"}` from pod start to the window end, paged
  by time slices until a slice returns fewer traces than the limit (no
  silent cap: a slice that still hits the limit at the smallest slice size
  is FAIL); XREADGROUP poll roots are scanned on a sample of 50 per language
  and then excluded. Connection-time roots from the first 5 minutes after
  each pod start, with fully anchored TraceQL regexes:
  `{resource.otel_libs.run="<run>" && (name=~".*connect.*" || name=~"(?i)(auth|hello|startup)")}`.
  > *Corrected 2026-11-18 (M7, owner decision option A):* the span set and
  > the Loki scan cover the run window plus the first 300 s after each pod
  > start (`status.startTime` and the latest container start of each pod;
  > earlier restarts of the same pod are not covered), merged where they
  > touch (`verify.py` `scan_intervals`, `STARTUP_SCAN`), not "from pod start
  > to the window end". Every root in those startup intervals is listed, so the
  > name-filtered connection-time query above is no longer a separate search.
  > The three server-side token counts below keep the full range from the
  > earliest pod start of the language (one search each, at most 1000 traces
  > fetched per search).
  Server-side TraceQL counts —
  `{resource.otel_libs.run="<run>" && statusMessage=~".*(dup|rej|unh)-<run>.*"}`,
  the same on `event.exception.message` and `span.db.query.text` — are
  inputs to the g.sentinel cells below, not a separate must-be-0 gate. Every
  span value, attribute, event attribute and status description is scanned.
  **Loki**: the run's records — `{k8s_namespace_name="dev-otel-libs", k8s_pod_name=~"<run pods>|demo-postgres-.*|demo-redis-.*"}`
  over the run interval, fetched in time slices — scanned **client-side**
  over the line **and every structured-metadata field** (a LogQL line filter
  does not see structured metadata, where `exception_message`,
  `exception_stacktrace`, `db_query_text`, `url_full` live — audit:
  `|~ "Simulated API error"` → 0 records, `| exception_message=~".*Simulated API error.*"`
  → 21; without the categorize-labels header Loki merges structured metadata
  into `stream`, as `loki_lines` already reads it, `verify.py:95-110`).
  Optional server-side label filters on `exception_message`,
  `exception_stacktrace`, `db_query_text`, `url_full` as cross-checks. The
  pod scope is required: a namespace-wide 48-hex search already matches other
  workloads' pods in `dev-otel-libs` (audit). Output: counts and labels only;
  `verify.py` never reads a Secret. Cells: **g.secret** (48-hex token,
  `password=`, userinfo — spans and Loki); **g.sentinel.db** (any run token on
  DB client spans — in practice `dup-<run>`); **g.sentinel.app** (any run
  token on every other span — in practice `rej-<run>` / `unh-<run>` on HTTP
  and gRPC spans, but a token on a demo span or `dup-<run>` on a non-DB span
  also lands here). The sentinel cells are about spans only; run tokens in log
  records are intended (ADR-014).
  *(r3.2, 2026-11-16: DE0e LOW 1 — every span belongs to exactly one cell.)*
  *(r3.1, 2026-11-16: H2 — on `unhandled` the A37-owned instrumentations copy
  `boom unh-<run>` into spans (root `README.md:367`), so one g.sentinel cell
  mixed A36 and A37; split.)*
*(r2, 2026-11-16: review H2, H3, H4, H5 and LOWs.)*
*(r3, 2026-11-16: audit #3/#4 and N4 — Loki scan client-side over structured
metadata, scoped to the run's pods and the two backing services, anchored
token; audit #5 — b selectors; #6 — severity number and flags; #7 and N5 —
d.grpc allows the constant client description, run-token scan; #8/#9 —
`connection_type=""` edges, sum over `failed`, failed shares, messaging
count and TraceQL link count, `expired_edges` dropped; #10 — structural
metric check, `target_info` join; #11 — OTLP-only Loki selector, counts,
window sampling; N6 — g paging, poll-root exclusion, anchored TraceQL
regexes, server-side cross-checks; #16 — settle delay justified; #17 — 4xx
/ 500 span status rules, `db.response.status_code`; #21 — run scoping by
`target_info`.)*
*(r3.3, 2026-11-16: three findings of the observability scorecard on the
examples run at `bedfa19` are carried into check a and a new informational
line, before DE6. (1) **Late root spans**: long-lived root spans reached
Tempo after their child logs. A trace id or `span_id` from check a that does
not resolve is retried once after 60 s; only the second miss is FAIL, and the
retry count is printed. (2) **Client connection resets**: the examples'
process → api calls showed connection resets in Python, Node.js and .NET,
probably a client keep-alive racing the server idle timeout (unproven). Check
d prints an INFO line per language with the count of worker → api HTTP
client spans whose `error.type` is a connection error, never FAIL; a non-zero
count is analysed in DE6 and the cause recorded in DE-D7. (3) **.NET gRPC
client exemplar**: on .NET the exemplar of the gRPC client duration points at
an `HttpClient` span that is never exported (parked item in
`specs/dotnet-otel-helper/tasks.md`, commit b572e3a). Check b reads server,
DB and demo series only, so it is not hit; if a client series is added
(DE7), the .NET cell is a strict xfail on the task DE8 creates for it.)*
*(r3.6, 2026-11-16, observability scorecard of the first live run
`20261116t1812`, overall ≈ 8.7/10, no BLOCKING/HIGH. (1) **Startup race**,
not keep-alive: the r3.3 "connection resets" (one worker → api error in
Python, Node.js and .NET, two Go api → backend `UNAVAILABLE`) all fell in the
first 12 s after pod start; 0 inside the window. So the worker waits, before
its first `demo.drive`, until the api's `GET /health` answers 200, and the
api's `/health` answers 200 only when Redis is connected **and** the backend's
`grpc.health.v1` answers SERVING; `verify.py demo` reports BLOCKED when the
window starts less than 60 s after the youngest app pod's start. The INFO
line stays. (2) The XREADGROUP poll roots (≈ 27 per minute per language) are
noise: the worker suppresses tracing around the empty poll (no span when no
message arrives). *(Refined 2026-11-16 after DE3: the `XREADGROUP` call is
untraced always — it runs before any producer context exists, so it cannot
sit under the CONSUMER span; the CONSUMER span and its `XACK` stay traced.
The start-up group creation is untraced too where the language allows.)* (3) New tasks from the run: A42 (.NET ASP.NET Core server
`error.type` = exception type name, so .NET d.http stays xfail on A37 and
A42), A43 (Go and .NET DB-client exemplars on the parent span; b.db_client
keeps "a span of the trace" until A43 is decided); A37 evidence extended.
(4) `otelcol_connector_servicegraph_dropped_spans` returns no data on this
gateway: the info line prints `unmeasured` instead of 0.)*
*(r3.7, 2026-11-16, DE5 code review: (1) an `xfail` cell carries a `reason`
list in `expected.json`; it is XFAIL only when every observed problem contains
one of the reasons, any other problem makes it FAIL (so an xfail cannot hide a
new defect). (2) An `n/a` b cell marked `requires_series` (Node.js: metrics
without SDK exemplars) is NA only while the metric's series exist. (3) Check a
counts toward the minimum only the records on a demo span or below one in the
same service (estimated as total × the sampled demo share; ≥ 1 per service).
(4) An exemplar or record without `span_id`, or with an id that does not
decode, is a problem. (5) Check g: the hygiene token also matches upper-case
hex, resource attributes are scanned, hits print the span kind, span id and
field label only; Loki slices share their midpoint (records de-duplicated);
Tempo paging is cross-checked against a TraceQL `count_over_time()` of root
spans for one aligned minute, and a mismatch is a problem. (6) The grace check
uses each pod's latest start (container restarts included), the g scan the
earliest `status.startTime`. (7) Service-graph edges are not run-scoped (no
run label); the report line says so.)*
**Justification**: read-only through the API server proxy like the existing
modes (`testing/k8s/README.md:181-184`); strict xfail per ADR-008; minimums
from the deterministic cycle (DE-D7) make empty data a failure.
**Trade-offs**: full paging of the run's traces through the proxy is slow
(time `unmeasured`); the 48-hex rule may flag an unrelated value (a hit is
investigated, never allow-listed silently).
**When it would be wrong**: Tempo does not index `span.demo.scenario`, or the
proxy times out at the smallest slice.
**Reversal action**: search by `rootName="demo.drive"` and filter
client-side; run the checker as a Job (soak K-D3 reversal).
**Alternatives**: Grafana API (token); assertions inside the apps.

> *Amended 2026-11-18 (D16 cluster runs, code-review approved):* a backend
> request that fails in transport (API server unreachable, connection reset,
> TLS handshake or i/o timeout, kubectl's 60 s timeout) is retried up to
> three times, 1 s and 3 s apart; an HTTP error answer (`Error from server`)
> or a non-JSON answer is never retried. In a hard API-server outage a run
> therefore takes up to about three times longer to fail. The tally line
> counts pre-flight BLOCKED lines (`…; pre-flight BLOCKED N`); the exit code
> rule is unchanged. Open (owner decision pending): check g scans from the
> earliest pod start (M7), which on pods about 14 h old means about 10 000
> trace fetches and about 19 minutes; proposed: bound the g span set to the
> window and keep the startup roots as their own query.
>
> *Decided 2026-11-18 (owner, option A):* check g scans the span set of the
> run window only (the traffic `verify` itself sends, which covers every
> route), plus a separate query for the first minutes after each pod start
> (startup roots: configuration and connection logs and spans). Trade-off
> accepted: a leak that appears only in traffic outside both windows is not
> seen by `verify`.

### DE-D12 — Resource budget and concurrency
**Choice**: requests and memory limits of the Budget table; `demo.sh apply`
runs the soak K-D2 pre-flight — Σ run requests ≤ allocatable − requested −
reserve (250 m / 512 Mi), pod-slot guard — and starts all four languages when
it fits, else one language per window with PostgreSQL and Redis kept up
(ruling DE-O8). The `FailedScheduling` watch has the K-D2 scope: events for
pods outside `dev-otel-libs`, still `Pending`, newer than the run start,
message containing `Too many pods`, `Insufficient cpu` or `Insufficient
memory`; on a hit the run scales its Deployments to 0. No CPU limit; memory
limits set.
*(r2, 2026-11-16: watch scope stated, review LOW; new node reads below;
ruling DE-O8.)*
**Justification**: node reads 2026-11-16 (`kubectl describe node k8s01`,
requests of 8 CPU / 31941 Mi allocatable): r1 ≈ 13:50 UTC 4685 m (58 %) /
23584 Mi (73 %), 107 pods; the coordinator's later read 4825 m / 24336 Mi,
112 pods; r2 ≈ 14:20 UTC 4580 m (57 %) / 22912 Mi (71 %). The soak's
2026-11-15 reads were 7395–7595 m. The margin moves, so the pre-flight reads
it every run. Requests come from the example apps' measured peaks
(2 days, `max_over_time(container_memory_working_set_bytes[2d])` and the
5-minute CPU rate, `dev-otel-libs`, 2026-11-16): memory Go 8–26 Mi, Python
43–64 Mi, Node.js 40–52 Mi, .NET 58–79 Mi per app; CPU ≤ 1.4 m Go, ≤ 7.4 m
Python, ≤ 4 m Node.js, 15–19 m .NET api/backend, 71–85 m the .NET `process`
heavy worker. Driver overhead `unmeasured` until DE7.
**Trade-offs**: requests below real use take unrequested CPU from neighbours
(node CPU limits overcommitted, 179 % at the r2 read).
**When it would be wrong**: a platform `FailedScheduling` during a run, or
DE7 finds a container above its request.
**Reversal action**: resize from DE7 (dated note), rotate, shorten.
**Alternatives**: always rotate; compact layout.

### DE-D13 — Credentials generated per run, never printed, never logged
*(r3.3: "per run" is superseded — see **Lifetime** below.)*
**Choice**: `demo.sh apply` generates the PostgreSQL superuser password, one
password per language role and the Redis password with `openssl rand -hex 24`
into shell variables (`set +x` around it), and creates the Secrets from a
manifest with `stringData` on stdin with `kubectl create -f -` (a re-run
deletes them first; r3.3: see **Lifetime** — a re-run while the services run reuses them) or `kubectl apply --server-side -f -` — never client-side
`kubectl apply`, which would store the plaintext in the
`last-applied-configuration` annotation, and never `--from-literal` (process
list).
- **Redis**: the Secret holds a `redis.conf` with `requirepass <pw>`, mounted
  as a file; `redis-server /etc/redis/redis.conf`. Probes run `redis-cli
  ping` with `REDISCLI_AUTH` from `secretKeyRef`, never `-a`. *(r3.3:
  superseded — see **No password in container env** below.)*
- **PostgreSQL**: the superuser password through the image's
  `POSTGRES_PASSWORD` from `secretKeyRef` (r3.3: superseded by
  `POSTGRES_PASSWORD_FILE`, see **No password in container env**); the per-language roles are
  created with a **SCRAM-SHA-256 verifier** computed on the host by a
  `python3` standard-library helper (`hashlib.pbkdf2_hmac('sha256', …, 4096)`
  plus `hmac` for the client and server keys, RFC 5802 / 7677) that reads the
  password on **stdin** — never `openssl kdf -kdfopt pass:…` (the password
  in the process list; LibreSSL on macOS has no `kdf` command). The
  verifier SQL (`CREATE ROLE demo_<lang> LOGIN PASSWORD 'SCRAM-SHA-256$4096:…'`)
  is written into an initdb script held in a Secret and mounted at
  `/docker-entrypoint-initdb.d/`, so the server never sees a role's
  plaintext; the init session runs with `SET log_min_error_statement =
  panic` so a failing init statement is not echoed to the log.
- **Apps** read passwords with `secretKeyRef` and build DSNs in memory; DSNs
  are never logged. `demo.sh render` prints `<generated>` in place of
  values; `demo.sh delete` deletes the Secrets (r3.3: `delete --all`; see
  **Lifetime**); `verify.py` never reads them.
  *(r3.3, 2026-11-16, before DE3: superseded for the apps as for the
  backing services — no password in container env. Each app mounts its key of
  `demo-postgres-auth` (`demo_<lang>`) and the `password` key of
  `demo-redis-auth` as files, mode 0440, and reads the paths from
  `DEMO_PG_PASSWORD_FILE` and `DEMO_REDIS_PASSWORD_FILE` once at start-up;
  the other connection settings (`DEMO_PG_HOST`, `DEMO_PG_DB`, `DEMO_PG_USER`,
  `DEMO_REDIS_ADDR`, and `DEMO_API_URL` (worker → api), `DEMO_BACKEND_ADDR`
  (api → backend)) are plain env, as are the listen ports `DEMO_HTTP_PORT`
  (api, 8080) and `DEMO_GRPC_PORT` (backend, 50051). DE4 renders the mounts.)*
  *(r3.5, 2026-11-16, DE4: least privilege — the worker and the api mount
  only `demo-redis-auth/password`, the backend only
  `demo-postgres-auth/demo_<lang>`; `fsGroup` is each image's user. Probes:
  startup and readiness use api `GET /health` and backend `grpc.health.v1`;
  liveness is a TCP check, so a PostgreSQL or Redis blip makes a pod unready
  instead of restarting it; the worker has no probe (no listener).)*
- **Proof**: check g.secret (DE-D11): the anchored 48-hex scan over every
  span of the run and over the Loki records (line and structured metadata)
  of the run's pods and the two backing services.
*(r3, 2026-11-16: N7 — SCRAM tool and initdb Secret; proof scope follows
DE-D11 g, N4.)*
*(r2, 2026-11-16: review H6 — r1 left the Redis password on the command
line of probes, the role passwords in PostgreSQL statements and the Secret
in a client-side apply annotation.)*
- **No password in container env** *(r3.3, 2026-11-16, DE2 code review;
  replaces the `secretKeyRef` env choice of the Redis and PostgreSQL bullets
  above, which stay as written)*: PostgreSQL reads the superuser password
  from `POSTGRES_PASSWORD_FILE`, a file of the `demo-postgres-auth` Secret
  (item `POSTGRES_PASSWORD`, mode 0440, group = `fsGroup` 70); Redis mounts
  the `password` item next to `redis.conf`, and each probe runs
  `REDISCLI_AUTH="$(cat /etc/redis/password)" redis-cli ping`, so the value
  is in the probe's process environment only. No container `env` entry of
  the backing services refers to a password key. (The image entrypoint still
  exports the value read from the file into the postgres server's own
  process environment; that is not visible in the pod spec.)
- **Lifetime.** Passwords live as long as the backing services (`apply` →
  `delete --all`); a rotation window reuses them, and `apply` with an
  explicit `RUN` that differs from the running services' `otel-libs/run`
  fails.
- **Init check.** The initdb script runs once, on an empty PGDATA; a
  container restart during init leaves PGDATA half-initialized and the
  entrypoint then skips the script. After the rollout `apply` fails when the
  PostgreSQL container's `restartCount` > 0 or `pg_roles` holds fewer than the
  four `demo_<lang>` roles (count only, no secret), and says to `delete --all`
  and apply again.
  *Corrected 2026-11-25:* two k8s01 reboots left the PostgreSQL container at
  `restartCount` 2 after a complete init, and every `apply` (the soak hook and
  the trap's demo restore) failed. A restart no longer fails by itself: with
  `restartCount` > 0 the check also needs the initdb script's last effects —
  the four `demo_<lang>` databases and no `PUBLIC` `CONNECT` on `template1`
  (`has_database_privilege`, no secret). PGDATA is an `emptyDir`, kept across
  container restarts, and the script does not run again; the entrypoint runs
  it with `ON_ERROR_STOP=1` and the `template1` revoke is its last lasting
  statement, so the markers prove completion. Reversal action: a
  half-initialized PGDATA that passes these markers → check every statement's
  effect.
- **psql echo.** `log_min_error_statement = panic` keeps a failing init
  statement out of the **server** log; psql (the entrypoint's client) may
  still print the failing line — a verifier, never a password — to the
  container log.
*(r3.3, 2026-11-16, DE2 review: lifetime (was per run), init check, psql
client-side echo.)*
*(r3.3, 2026-11-16, DE2 apply on k8s01: the image's default `pg_hba.conf`
trusts `local` and `127.0.0.1/32`, `::1/128`; every other host uses
`scram-sha-256`. Kept: only processes inside the `demo-postgres` container
reach loopback (no shared network namespace, no service account token,
read-only root filesystem), and apps connect through the Service, where a
wrong password is denied (measured). Proofs of password checks must connect
through the Service, never over loopback. Reversal: `POSTGRES_INITDB_ARGS=
--auth-local=scram-sha-256 --auth-host=scram-sha-256`, with `verify_init`
authenticating.)*
**Justification**: AGENTS.md "Never print secrets"; the helpers drop
connection strings from spans (Go `go/ext/otelredis/redis.go:19-20`; .NET
`dotnet/README.md:636`; Node.js `nodejs/HOW-TO.md:624`), and g checks it.
**Trade-offs**: passwords are not recoverable after the run (data is
ephemeral); a crashed apply may leave a Secret until `delete`.
**When it would be wrong**: an external secret store becomes the owner's rule.
**Reversal action**: an ExternalSecret in the namespace.
**Alternatives**: fixed passwords (in git); `--from-literal` (process list).

### DE-D14 — Grafana datasource settings: recorded read-only; the platform applied the requested changes
**Choice**: the run report records the three datasource models (read-only
Grafana API, no secrets) and two manual Explore checks: (1) open an OTLP demo
record in Loki Explore and confirm whether the `trace_id` derived field links
to Tempo; (2) from a Tempo trace whose id has leading zeros (Tempo search
shows it stripped, e.g. `78eabb087f488163bc0d5cfc51b4f` for
`00078eabb087f488163bc0d5cfc51b4f`), use trace → logs and confirm the
`${__trace.traceId}` value matches Loki's 32-hex `trace_id`.
Read 2026-11-16 ≈ 14:20 UTC, after the platform change:
- **Prometheus** (`uid prometheus`): `exemplarTraceIdDestinations:
  [{name: trace_id, datasourceUid: tempo, urlDisplayLabel: "View trace"}]`.
- **Tempo** (`uid tempo`): `tracesToLogsV2: {datasourceUid: loki,
  customQuery: true, query: '{service_name="${__span.tags["service.name"]}"} | trace_id="${__trace.traceId}"',
  spanStartTimeShift: -5m, spanEndTimeShift: 5m}`; `filterByTraceID` removed.
- **Loki** (`uid loki`): `derivedFields: [{name: trace_id, matcherType: label,
  matcherRegex: trace_id, datasourceUid: tempo}]`. A `label` matcher applies
  `matcherRegex` as an **unanchored** regex to label keys
  (`getDerivedFields.ts:30-44` read at Grafana v12.3.0 by the audit; the
  cluster runs 13.3.1, where that path does not exist at the tag — the
  13.3.1 behaviour is `unmeasured`), so `trace_id` also matched a key such
  as `parent_trace_id`. The platform owner has since set `matcherRegex:
  ^trace_id$` (homelab-gitops change, 2026-11-16, recorded from the
  coordinator, not re-read here). Whether the field sees OTLP structured
  metadata is still `unmeasured` (check 1, DE9).
  *(r3.1, 2026-11-16: anchored regex applied by the platform; source-version
  caveat added.)*
*(r2, 2026-11-16: rulings DE-O2 / DE-O3 — requests applied by the platform
owner. r1 recorded the earlier state.)*
*(r3, 2026-11-16: audit #19 — unanchored label matcher and the `^trace_id$`
request; audit #20 — zero-padding Explore check.)*
**Justification**: the datasources are provisioned `readOnly: true`.
**Trade-offs**: the Explore steps are manual.
**When it would be wrong**: the owner wants the UI path gated.
**Reversal action**: a token-based Grafana API check, read-only.
**Alternatives**: a second Grafana in the namespace.

*(DE9, 2026-11-17, run `20261116t1812`, read-only: the three links resolve on
the data side. (1) Loki derived field `^trace_id$` (label matcher, structured
metadata among the frame's label keys) → Tempo trace `070460fb…` with the
record's `order.id` — correct trace. (2) Tempo → Loki with a leading-zero
trace: search shows `90d00dfd…` (31 hex), the span link expands
`${__trace.traceId}` to `090d00dfd5df9a790741f4a681debd8c` (32 hex), and that
Loki query returns the backend and worker records; the stripped id would
return nothing, so the padding matters and holds. (3) Prometheus exemplar on
`demo_order_duration_seconds_bucket` (.NET `016066c9…`, Go `c3b00ba8…`) →
Tempo, `span_id` = `demo.order.process`. The UI click itself (link button
shown) is not measurable through the API: one human click per check closes
it.)*

### DE-D15 — Local Compose path: optional, smoke only
**Choice**: `demo/compose.yaml` (task DE10) runs one language's apps,
PostgreSQL, Redis and a Collector whose config exports traces and logs to the
`debug` exporter and metrics, through an `otlphttp` exporter, to a local
Prometheus started with `--web.enable-otlp-receiver` (the repo's
`testing/docker-compose.yml` Collector has only the debug exporter, so DE10
adds its own Collector config). No Tempo or Loki; `verify.py` does not run
against it; no acceptance item depends on it.
*(r2, 2026-11-16: ruling DE-O5 — optional, no acceptance; the exemplar
fallback role is removed (DE-D10); the Collector → Prometheus path stated,
review LOW.)*
**Justification**: Compose users keep a local path; the apps are env-only.
**Trade-offs**: a second run path to keep working.
**When it would be wrong**: nobody uses it.
**Reversal action**: delete `demo/compose.yaml` in DE12.
**Alternatives**: kind (heavier, soak K-D1).

## Scenario table (reference)

| Scenario | Cycle positions | api | backend | gRPC status (constant message) | api HTTP | Owning span | Demo log record (inside the owning span) |
|---|---|---|---|---|---|---|---|
| `ok` | 1,2,4,5,7,8,10 | cache GET (miss → SET), PlaceOrder, PRODUCER + XADD | `demo.order.process`: upsert + SELECT, metrics, INFO `order processed` | OK | 201 | — | INFO `order processed` (backend), INFO `order event processed` (worker CONSUMER) |
| `business_error` | 3 | PlaceOrder | `OrderRejectedError("amount over limit rej-<run>")` in `demo.order.process` | FAILED_PRECONDITION (`order rejected`) | 422 | backend `demo.order.process` | ERROR + stack (backend) |
| `db_error` | 6 | PlaceOrder | INSERT `dup-<run>` → trigger `23505` `duplicate order dup-<run>` → `OrderExistsError("order dup-<run> exists")` | ALREADY_EXISTS (`order exists`) | 409 | backend `demo.order.process`; the DB CLIENT span is checked as d.db | ERROR + stack (backend, demo); helper ERROR record for the DB span (.NET today; others after A36) |
| `unhandled` | 9 | `DemoUnhandledError("boom unh-<run>")` thrown in `demo.order.accept`, before the cache | — | — | 500 | api `demo.order.accept` | ERROR + stack, written in `demo.order.accept` before the rethrow; the top-level handler only maps to 500 |

`demo.drive` (worker) is ERROR for the three error scenarios (DE-D8 rule)
but is not the owning span.
*(r3, 2026-11-16: run-token messages, log inside the owning span — N2, audit #7.)*

## Expected trace (reference, `ok`)

| # | Service | Kind | Name (pattern) | Parent | Key attributes |
|---|---|---|---|---|---|
| 1 | worker | INTERNAL | `demo.drive` | none | `demo.scenario=ok`, scope `otel-libs-demo` |
| 2 | worker | CLIENT | `POST` (Go `HTTP POST`, `specs/config-contract-v0.3/tasks.md:103`) | 1 | `http.request.method` |
| 3 | api | SERVER | `POST /orders` | 2 | `http.route=/orders` |
| 4 | api | INTERNAL | `demo.order.accept` | 3 | scope `otel-libs-demo` |
| 5 | api | CLIENT | Redis `GET` / `SET` | 4 | `db.system.name=redis` |
| 6 | api | CLIENT | `demo.v1.OrderService/PlaceOrder` | 4 | Python, Node.js legacy `rpc.*` (root `README.md:312`) |
| 7 | backend | SERVER | `demo.v1.OrderService/PlaceOrder` (.NET RPC-shaped with the DE-D2 key) | 6 | — |
| 8 | backend | INTERNAL | `demo.order.process` | 7 | `demo.scenario`, `order.id` |
| 9 | backend | CLIENT | DB spans (Metric map row "DB span names") | 8 | `db.system.name=postgresql`; `db.namespace` per language |
| 10 | api | PRODUCER | `send demo:<lang>:orders` | 4 | DE-D6 |
| 11 | api | CLIENT | Redis `XADD` | 10 | `db.system.name=redis` |
| 12 | worker | CONSUMER | `process demo:<lang>:orders` | 10, link → 10 | DE-D6 |
| 13 | worker | CLIENT | Redis `XACK` | 12 | — |

## Metric map and per-language names (reference)

Exemplar selectors are the `_bucket` series of each duration (DE-D10).

| Item | Go | Python | Node.js | .NET |
|---|---|---|---|---|
| HTTP server duration (api) | `http_server_request_duration_seconds_bucket` | same | same | same |
| RPC server duration (backend) | `rpc_server_call_duration_seconds_bucket` (root `README.md:312`) | `n/a:upstream-no-rpc-metrics` (`README.md:312`) | `n/a:upstream-no-rpc-metrics` | in `http_server_request_duration_seconds_bucket{http_route="/demo.v1.OrderService/PlaceOrder"}` (`README.md:318`) |
| DB client duration (backend) | `db_client_operation_duration_seconds_bucket` (XSAM v0.44.0 `dbconv.ClientOperationDuration`, `instruments.go:22,41,48`) | `n/a`: contrib SQLAlchemy has no operation duration — `unmeasured`, DE6 confirms | `db_client_operation_duration_seconds_bucket` (`nodejs/HOW-TO.md:627`) | same, meter `Npgsql` (`dotnet/README.md:618`, `dotnet/TESTS.md:776`) |
| Custom histogram | `demo_order_duration_seconds_bucket` | same | same | same |
| SDK exemplars | yes — measured end to end on the Go examples (DE-D10) | yes (`metrics.py:498`), `unmeasured` | `n/a:upstream-no-sdk-exemplars` (`README.md:329`) | yes (`dotnet/HOW-TO.md:613`), `unmeasured` |
| HTTP client span name (worker) | `HTTP POST` (otelhttp v0.72.0 default formatter; `specs/config-contract-v0.3/tasks.md:103`) | `POST` | `POST` | `POST` |
| DB span names | XSAM method names, e.g. `sql.conn.exec`, `sql.conn.query` (`conn.go:104,143`) — recorded at DE6 | contrib SQLAlchemy names — `unmeasured` | `pg.query:<OPERATION> demo_node` (`nodejs/HOW-TO.md:625`) | Npgsql names — `unmeasured` |
| PostgreSQL edge | DE-D4 table | | | |
| DB span `error.type` on `23505` | `unmeasured` | `unmeasured` | `_OTHER` (`nodejs/HOW-TO.md:617-620`) | the SQLSTATE expected (`dotnet/TESTS.md:776`: `22P02` as `error.type`) |
| DB span `db.response.status_code` | `23505` expected, `unmeasured` | same | absent (pg gives the wrapper no code, `nodejs/HOW-TO.md:617-620`) → `unmeasured` | `23505` expected |

## Id encodings (reference)

| Source | Trace id | Span id | Normalization in `verify.py demo` |
|---|---|---|---|
| Tempo `/api/traces/<id>` JSON | base64 | base64 | decode to hex, `zfill(32)` / `zfill(16)` |
| Tempo `/api/search` | hex, leading zeros stripped (audit: 10 of 129 ids had 31 characters) | — | `zfill(32)` |
| Loki structured metadata `trace_id`, `span_id` | 32 hex with zeros | 16 hex with zeros | lower-case |
| Prometheus exemplar labels `trace_id`, `span_id` | 32 hex with zeros | 16 hex with zeros | lower-case |
| Grafana `${__trace.traceId}` | 32 hex with zeros — the Tempo plugin hex-encodes the stored 16 bytes (`pkg/tempo/trace_transform.go:124-125`); measured DE9 2026-11-17 | — | — |

*(r3, 2026-11-16: new section, audit #20.)*

## Expected-results matrix (reference; predictions, replaced by DE6's measurements in `expected.json`)

> **r3.5 (2026-11-16), after the DE3 review (code-review + observability
> refutation of the four languages' apps and smokes).** Rulings, pinned for
> all four languages:
> - **Cache scope**: the api reads the quote cache (GET, SET on a miss) for
>   every scenario that reaches it — all but `unhandled`, which fails before
>   the cache. Supersedes the Scenario table's "PlaceOrder only" for
>   `business_error` / `db_error`. No `demo.cache.hit` attribute.
> - **api answers**: 422 / 409 for the two business gRPC codes; any other gRPC
>   failure → 502, `demo.order.accept` ERROR only (propagate, no event/log).
>   Cache and publish failures are not fatal: one WARN log, the answer stays
>   201. The PRODUCER span records no exception event of its own.
> - **Backend unexpected failure**: recorded in `demo.order.process` (ADR-014
>   pattern), `demo.outcome=error`, `error.type` = the exception type name,
>   gRPC INTERNAL with the constant message `order failed`.
> - **Transport `error.type` on `demo.drive`**: the exception type name (Go:
>   `%T` of the root error), never `_OTHER`.
> - **Framework logs on the `unhandled` path** (Python `uvicorn.error`, .NET
>   Kestrel): accepted as framework debt under A37, not filtered and not
>   caught (catching would also hide the framework span event that A37
>   measures). Check a/d count only records whose span id is a demo span.
> - **Values**: customers `cust-a`, `cust-b`, `cust-c`; `ok` amount
>   `10 + seq mod 90`; `seq` starts at 1; the worker runs at a fixed rate (a
>   tick, not sleep-after-work); `db_error` is a plain `INSERT` (not the
>   upsert); stream entries carry `order_id` and `scenario` only; the
>   consumer group is created at `$` with `MKSTREAM`, and a process that finds
>   it existing (BUSYGROUP) or missing (NOGROUP) recovers without an ERROR
>   span; `order event processed` carries `order.id` and `demo.scenario`;
>   the backend validates `scenario` (INVALID_ARGUMENT), the api answers 400.
> - **Base images** pinned by digest in every language.
> - **Matrix corrections** (measured in the four smokes, to be confirmed in
>   DE6): Go d.http → `xfail:A40` (otelhttp v0.72 sets no `error.type` on
>   server spans, `internal/semconv/server.go:74-75`); Python and Node.js
>   g.sentinel.db → pass (no run token on the DB span; d.db stays
>   `xfail:A36`: no event with `exception.type`); the d.http span set is the
>   SERVER span **plus the framework INTERNAL spans of the same request**
>   (Node.js express `request handler`), so Node.js d.http stays
>   `xfail:A37`; .NET d.grpc and e → pass (were `unmeasured`).

New task ids (`A38`, `A39`) are created by DE8 in
`specs/config-contract-v0.3/tasks.md` before `expected.json` cites them.

| Check item | Go | Python | Node.js | .NET |
|---|---|---|---|---|
| a logs → traces | pass | pass | pass | pass |
| b exemplars (per emitted metric) | pass (measured on the examples, DE-D10) | pass (no DB metric: `n/a`) | `n/a:upstream-no-sdk-exemplars` | pass |
| c custom metrics | pass | pass | pass | pass |
| d.demo (all three error scenarios) | pass | pass | pass | pass |
| d.grpc business_error / db_error | pass (client description = the constant gRPC message, allowed; otelgrpc v0.72.0 `stats_handler.go:304-306,386-389`, audit) | pass since 2026-11-19 (was `xfail:A37` — reason: the aio **server** span records `AbortError` on `context.abort` (`_aio_server.py:107-114`, DE-D8); the aio client records no exception (`_aio_client.py:95-115`)) | pass since 2026-11-19 (was `xfail:A37`: `grpc.error_message`, `README.md:367`) | `unmeasured` |
| d.http unhandled (+ 4xx rules) | pass (no event, no description, `README.md:367`) | pass since 2026-11-19 (was `xfail:A37`: FastAPI middleware event + description, `README.md:367`) | pass since 2026-11-19 (was `xfail:A37`: http/express `recordException` + description, `README.md:367`) | `xfail:A42` + `C20` since 2026-11-19 (was `xfail:A37`: ASP.NET Core `RecordException`, `README.md:367`, `dotnet/HOW-TO.md:811-815`) |
| d.db db_error | pass since 2026-11-19 (was `xfail:A36`: XSAM `span.RecordError`, `utils.go:57`) | pass since 2026-11-19 (was `xfail:A36`) | pass since 2026-11-19 (was `xfail:A36`) | pass (`dotnet/README.md:640`) |
| e trace structure | pass | pass | pass | `unmeasured` (Redis spans from the profiler, `dotnet/README.md:598-603`) |
| e PostgreSQL edge (`database` required) | `xfail:A38` (`virtual_node`, server `postgresql`) | pass, server `demo_python` (`unmeasured`) | pass, server `demo_node` | pass, server `demo_dotnet` (`unmeasured`) |
| e HTTP/gRPC edges, messaging edge | pass | pass | pass | pass |
| g.secret | pass | pass | pass | pass |
| g.sentinel.db (`dup-<run>` on DB spans) | pass since 2026-11-19 (was `xfail:A36`: XSAM `span.RecordError`, `utils.go:57`) | pass (DE6) | pass (DE6) | pass |
| g.sentinel.app (`rej-` / `unh-<run>` on HTTP/gRPC spans) | pass (no event, no description on the server; client description is the constant gRPC message) | pass since 2026-11-19 (was `xfail:A37`: FastAPI event + description; aio server records `AbortError`, which carries no run token — the `unh-<run>` path is the HTTP one) | pass since 2026-11-19 (was `xfail:A37`: http/express `recordException` + description, `README.md:367`) | `xfail:C20` since 2026-11-19 (was `xfail:A37`: ASP.NET Core `RecordException` event, `README.md:367`) |

`A39` is reserved for the first gap DE6 finds that no task owns.
*(Ruling 2026-11-19, contract task A36 review, observability: the d.db check
no longer requires an `exception` event. A failed DB span passes with **at
most one** `exception` event whose only attribute is `exception.type`, or
none: Python statement spans record none (contract A36 decision (3)), and
the correlated ERROR record carries the detail. `verify.py` `_d_db` and
`demo/test_verify_demo.py` changed accordingly; the other `_d_db` checks
(empty status description, `db.response.status_code` 23505, the correlated
record) stay. The `xfail:A36` cells in `expected.json` stay until a k8s01
run with the A36 images measures them; with the fixture's synthetic Loki
records the Python d.db cell already passes.)*
*(Measured 2026-11-19, contract task A36 closed: a k8s01 run with images
built from 84372c0 (Go images tagged `84372c0-ctxfix`) gave XPASS on
`go/d.db`, `go/g.sentinel.db`, `python/d.db` and `node/d.db` (summary
`NA 6, PASS 46, XFAIL 8, XPASS 4`); the four cells are `pass` in
`expected.json` and no `xfail:A36` cell remains. The recorded fixtures
(2026-11-18) predate A36, so `demo/test_verify_demo.py` now asserts that
their Go and Node.js DB spans judge FAIL: a regression to the old exception
detail fails the run.)*
*(Measured 2026-11-19, contract task A37 closed: a k8s01 run (run
`20261116t1812`) with Go, Python and Node.js images built from 3f36074
(.NET unchanged at `8e19973`) gave XPASS on `python/d.grpc`,
`python/d.http`, `python/g.sentinel.app`, `node/d.grpc`, `node/d.http` and
`node/g.sentinel.app` (summary `NA 6, PASS 50, XFAIL 2, XPASS 6`); the six
cells are `pass` in `expected.json` and no `xfail:A37` cell remains. The
.NET cells keep `xfail:A42` + `C20` (`d.http`) and `xfail:C20`
(`g.sentinel.app`). A first run after a rolling update judged both
`g.sentinel.app` cells XFAIL on spans of the replaced pre-A37 pods: check g
scans `STARTUP_SCAN` seconds after every pod start of every language
(`run.latest_starts`), so an interval opened by one language's rollout
covers another language's old pods; rotating the other languages with
`demo.sh delete --lang` / `apply --lang` (same images) so that every pod
started after the last old-pod span gave 0 hits. The recorded fixtures
(2026-11-18) predate A37, so `demo/test_verify_demo.py` asserts that their
Python and Node.js `d.grpc` / `d.http` judge FAIL.)*
*(r3, 2026-11-16: Go d.grpc corrected — r2 cited otelgrpc's server-side
description rule for a client-span cell (audit #7, N5); PostgreSQL edge
row per the coordinator ruling (N3, audit #12); Python d.grpc evidence
re-scoped to the aio client (N8).)*
*(r3.1, 2026-11-16: g.sentinel split into `.db` / `.app` (H2): r3 predicted
.NET pass and Python/Node.js `xfail:A36`, but the `unh-<run>` token reaches
A37-owned spans; Python d.grpc reason moved to the aio server span (M1).)*

## Relation to Phase K (reference)

*(Amended 2026-11-19, soak r6: the soak now runs on the demo apps — they are
its workload in all four languages, through the DE-D7 env-override hook; the
K6 caveat below is handled there by counting at a dedicated Collector copy
and judging only the api → backend service-graph edge.)*

The demo does not change `specs/soak-simulation/`. It can feed:
- **K2**: twelve single-container pods with the agent's attributes in Tempo,
  Loki and Prometheus for every language (K-D4).
- **K5**: real `job` / `instance` / promoted labels per language, including a
  `_total` counter and an annotation unit (K-D9).
- **K6** — not exact: the deterministic cycle gives the expected count per
  route, but the connector's `store.ttl: 10s` is shorter than the possible
  producer/consumer arrival skew (two independent 5 s batch stages plus SDK
  batching, ≈ 17 s worst case, `unmeasured`), and
  `otelcol_connector_servicegraph_expired_edges` has no per-edge labels, so
  missing messaging edges cannot be attributed. K6 can use the demo's HTTP
  and gRPC edges with a tolerance it calibrates, not as exact counts.
  *(r2, 2026-11-16: r1 said "exact", review LOW.)*
- **K3** partly (new `service.instance.id` per pod restart; no restart
  scenario). **K7**, **K8** not (K-D6; soak apps and windows).

## Budget table (reference)

Proposed requests and limits; DE7 recalibrates. Basis: DE-D12.

> **DE7 calibration (2026-11-17, run `20261116t1812`, measured 02:17–02:30
> UTC over 90 min for the apps and 8 h for PostgreSQL and Redis).** Request ≈
> p95 + ≥ 20 % headroom of the busiest app per language; memory limit ≥ 2× the
> highest maximum across both pod generations; no CPU limit. New values (in
> `testing/k8s/demo/demo.sh`): Go 5 m / 32 Mi / 64 Mi; Python 15 m / 96 Mi /
> 192 Mi; Node.js 10 m / 64 Mi / 192 Mi; .NET 20 m / 128 Mi / 256 Mi;
> `demo-postgres` 20 m / 64 Mi / 256 Mi; `demo-redis` 25 m / 32 Mi / 128 Mi.
> All four + shared: **195 m / 1056 Mi** requests (was 450 m / 1424 Mi),
> 2496 Mi limits; with the K-D2 reserve 445 m / 1568 Mi. Measured use ≈ 74 m
> CPU (Σ p95) and 697 Mi working set (Σ max). Node at the read: 3275 m /
> 7797 Mi free, 78 pod slots — fits. .NET memory still grew 7–13 Mi per app in
> the last hour (others ±2.6 Mi): recheck over several hours; .NET keeps its
> 128 Mi request until then. The table below is the r2 proposal, kept for the
> record.

| Component | Pods | CPU request | Memory request | Memory limit | Σ CPU | Σ memory request |
|---|---|---|---|---|---|---|
| Go worker / api / backend | 3 | 20 m | 48 Mi | 128 Mi | 60 m | 144 Mi |
| Python | 3 | 20 m | 96 Mi | 256 Mi | 60 m | 288 Mi |
| Node.js | 3 | 20 m | 96 Mi | 256 Mi | 60 m | 288 Mi |
| .NET | 3 | 40 m | 128 Mi | 384 Mi | 120 m | 384 Mi |
| `demo-postgres` | 1 | 100 m | 256 Mi | 512 Mi | 100 m | 256 Mi |
| `demo-redis` | 1 | 50 m | 64 Mi | 128 Mi | 50 m | 64 Mi |
| **All four + shared** | 14 | | | | **450 m** | **1424 Mi** |
| + K-D2 reserve | +1 slot | | | | 700 m | 1936 Mi |
| One language (.NET) + shared | 5 | | | | 270 m | 704 Mi |
| Free at the r2 read (2026-11-16 ≈ 14:20 UTC) | ≈ 88 slots | | | | ≈ 3420 m | ≈ 9029 Mi |
