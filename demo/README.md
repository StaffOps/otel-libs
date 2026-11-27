# otel-libs demo — shared contract

The demo runs the same three apps in every language — `worker` → `api` →
`backend` — against one shared PostgreSQL and one shared Redis on the test
cluster, to validate log/trace correlation, exemplars, custom metrics and the
ADR-014 split of exception detail (error on the span, stack in the log). The
spec is the contract: [`specs/demo-environment/`](../specs/demo-environment/)
([requirements](../specs/demo-environment/requirements.md),
[design](../specs/demo-environment/design.md),
[tasks](../specs/demo-environment/tasks.md)).

This directory holds what every language shares:

| Path | What |
|---|---|
| [`proto/otellibs/demo/v1/demo.proto`](proto/otellibs/demo/v1/demo.proto) | gRPC contract (`demo.v1.OrderService`: `PlaceOrder`, `GetOrder`), DE-D3 |
| [`sql/schema.sql`](sql/schema.sql) | `orders` table and the `BEFORE INSERT` trigger that raises `23505` for `dup-%` ids, DE-D4; applied by each backend at start |

Per language:

- **Go** — module `go/demo` (own `go.mod`, `replace` lines to the local
  helper modules); stubs committed in `go/demo/gen/`. After changing the
  proto run `go/demo/gen.sh` (Docker, pinned protoc and plugins) and commit
  the result; CI job `demo-go-stubs` runs `go/demo/gen.sh check`.
- **Python** — [`python/demo/`](../python/demo/README.md): stubs generated
  from this proto at image build (`grpcio-tools`, removed from the runtime
  image) and at test time (`python/demo/run-tests.sh`); nothing committed.
- **Node.js** loads the proto at run time, **.NET** uses `Grpc.Tools` at
  build (tasks DE3c–DE3d).

Backing services on the cluster: `testing/k8s/demo/demo.sh apply | delete
--lang <lang> | delete --all | render` (task DE2); the apps with `apply --lang
<lang> | --all` and `scale --lang <lang> <n> | --all <n>`, their env overridable
through `DEMO_ENV_FILE` for the soak (task DE13, rules in
[`testing/k8s/README.md`](../testing/k8s/README.md#demo-env-overrides-and-scale-de13)). Connection contract for the
apps (namespace `dev-otel-libs`; passwords generated at `apply`, kept until
`delete --all`, never printed — DE-D13):

| Service | Address | Per language `<lang>` ∈ `go`, `python`, `node`, `dotnet` | Password (`secretKeyRef`) |
|---|---|---|---|
| PostgreSQL 17.11 | `demo-postgres:5432` | database and role `demo_<lang>` (connects to its own database only) | Secret `demo-postgres-auth`, key `demo_<lang>` |
| Redis 7.4.12 | `demo-redis:6379` | keys `demo:<lang>:quote:<customer>`, stream `demo:<lang>:orders` (DE-D4, DE-D5) | Secret `demo-redis-auth`, key `password` |

Build DSNs in memory from these and never log them. Status of the remaining tasks: [tasks.md](../specs/demo-environment/tasks.md).

## Check a run: `verify.py demo`

`testing/k8s/verify.py demo` reads one run back from Tempo, Loki and
Prometheus on the cluster and judges every language × cell against
[`testing/k8s/demo/expected.json`](../testing/k8s/demo/expected.json)
(design DE-D11). It is read-only: every query goes through the API server's
service proxy, and the pre-flight only `get`s Deployments and pods.

```bash
KUBE_CONTEXT=k8s01 python3 -I testing/k8s/verify.py demo --run "$RUN" --dry-run   # pre-flight only
KUBE_CONTEXT=k8s01 python3 -I testing/k8s/verify.py demo --run "$RUN" --window 15m --settle 2m
KUBE_CONTEXT=k8s01 python3 -I testing/k8s/verify.py --langs go,python demo --run "$RUN"
```

`$RUN` is the run id of `demo.sh apply` (pod label `otel-libs/run`, resource
attribute `otel_libs.run`). The window is `[now − settle − W, now − settle]`;
it must start at least 60 s after the youngest app pod start, or the
pre-flight is BLOCKED.

**Pre-flight** (INFO or BLOCKED lines): Prometheus `exemplar-storage` and its
start time, the exemplar buffer fill, `target_info` instances of the run (two
per app: the app's own and the one the gateway's span-metrics connector
writes, without an `instance` label), `OTEL_METRICS_EXEMPLAR_FILTER` unset in
the demo Deployments (DR6; env names only, never values), and the run's pods.

| Cell | Checks (requirement) |
|---|---|
| `a` | Loki records with a `trace_id` resolve in Tempo and their `span_id` is a span of that trace, on or below a demo span (DR5) |
| `b.http_server`, `b.rpc_server`, `b.db_client`, `b.demo_order` | `_bucket` exemplars of the run's instances resolve to a trace and a span; `b.db_client` also wants that span to be the DB CLIENT span (`db.system.name`, or the legacy `db.system`) (DR6) |
| `c` | `demo.orders.processed` and `demo.order.duration`: names, labels, the nine DE-D9 bounds, an increase per backend scenario, metric `error_type` = the pinned literal (DR7) |
| `d.demo` | owning demo span: ERROR, no description, pinned `error.type`, no `exception` event, exactly one ERROR record in the trace, on that span; propagating spans clean (DR3, A45) |
| `d.grpc`, `d.http`, `d.db` | the instrumentation spans of each error route: status, `error.type`, no exception detail on the span; on `unhandled` the api SERVER span is `500` and carries the expected ERROR records (Python and Node.js 1, Go 0, .NET the Kestrel record); the failed PostgreSQL span has `23505` and a correlated record (DR3, ADR-014) |
| `e.trace`, `e.edges`, `e.edge.db` | the `ok` trace shape, the service-graph edges worker → api → backend, the messaging edge, the PostgreSQL edge with `connection_type="database"` (DR8) |
| `g.secret`, `g.sentinel.db`, `g.sentinel.app` | no 48-hex token, `password=`, URL userinfo or run token in any span of the run (window + 300 s after each pod start) or in the pods' Loki records; labels only are printed (DR12) |

INFO lines that are reported, not gated: worker → api connection errors per
language, the api → Redis edges, the service-graph connector's dropped spans
(`0 (series absent, gateway job up)` when the counter was never created) and
its gateway-wide expired-edge share.

**Reading the verdicts.** `PASS`; `FAIL`; `XFAIL` — the cell is
`xfail:<task>` and every problem matches one of its `reason` strings (an
unexplained problem makes it `FAIL`); `XPASS` — an xfail cell passed: flip it
to `pass` (fails the run, so a fixed gap is never missed); `NA` — an
`n/a:<reason>` cell with no data (a metric the language does not emit; Node.js
cells marked `requires_series` are NA only while the series exist);
`UNEXPECTED` — data on an n/a cell; `BLOCKED` — the check could not run. Exit
`0` when every cell is PASS, XFAIL or NA, `1` otherwise, `2` for usage errors.
Every xfail names an open task (`tools/spec_lint.py` rule `xfail-task`); the
refs in `expected.json` say why each non-pass cell is what it is. More detail:
[`testing/k8s/README.md`](../testing/k8s/README.md#verify).
