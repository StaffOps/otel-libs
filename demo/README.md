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
