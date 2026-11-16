# Node.js demo apps (`demo-node-*`)

The Node.js set of the otel-libs demo — `worker` → `api` → `backend` with the
shared PostgreSQL and Redis — specified in
[`specs/demo-environment/`](../../specs/demo-environment/) (task DE3c). The
shared contract (proto, schema, connection table) is described in
[`demo/README.md`](../../demo/README.md).

| App | Service | Role | Helper packages |
|---|---|---|---|
| `worker/` | `demo-node-worker` | load driver (`POST /orders` every `DEMO_INTERVAL_SECONDS`, root span `demo.drive`, scenario cycle of DE-D7) and queue consumer (`XREADGROUP` without `BLOCK` every `DEMO_POLL_SECONDS`, CONSUMER span `process demo:node:orders` with a link to and parent = the producer) | core, `-redis` (node-redis), `-pino` |
| `api/` | `demo-node-api` | express `POST /orders` (span `demo.order.accept`), Redis quote cache, gRPC client of `PlaceOrder`, PRODUCER span `send demo:node:orders` + `XADD` | core, `-redis` (node-redis), `-pino` |
| `backend/` | `demo-node-backend` | gRPC `demo.v1.OrderService` (proto loaded at run time), span `demo.order.process`, `pg` upsert + read-back, metrics `demo.orders.processed` / `demo.order.duration` | core, `-sql` (pg), `-pino` |

Each app's `main.js` sets the helper up through `lib/bootstrap.js` —
`setup()` → `shutdown()` → `setup({ instrumentations })`, one
`service.instance.id` for both — and then loads `app.js`. The double setup
works around a helper defect (an opt-in factory plus OTLP over gRPC leaves
the core http, express and gRPC instrumentations without spans); remove it
when the helper is fixed.

`lib/` holds the logic the three share (scenario cycle, ADR-014 failure
recording, gRPC status mapping, messaging attributes and propagation, order
processing); it is unit-tested in `test/`.

## Environment

Every setting comes from the environment (DE-D2, DE-D13); passwords are read
once from files and never logged.

| Variable | App | Default |
|---|---|---|
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` (with `otel_libs.run=<run>`), `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_METRIC_EXPORT_INTERVAL` | all | helper defaults |
| `DEMO_API_URL` (base URL; `/orders` is appended) | worker | `http://demo-node-api:8080` |
| `DEMO_REDIS_ADDR` (`host[:port]`), `DEMO_REDIS_PASSWORD_FILE` | worker, api | `demo-redis:6379`; file required |
| `DEMO_INTERVAL_SECONDS`, `DEMO_POLL_SECONDS` | worker | `5`, `2` |
| `DEMO_HTTP_PORT`, `DEMO_BACKEND_ADDR` (`host:port`) | api | `8080`, `demo-node-backend:50051` |
| `DEMO_GRPC_PORT`, `DEMO_PG_HOST` (`host[:port]`), `DEMO_PG_DB`, `DEMO_PG_USER`, `DEMO_PG_PASSWORD_FILE` | backend | `50051`, `demo-postgres`, `demo_node`, `demo_node`; file required |
| `DEMO_SCHEMA_FILE` | backend | `/etc/otel-libs-demo/schema.sql` (baked into the image) |

The run id in the scenarios' error messages (`rej-<run>`, `dup-<run>`,
`unh-<run>`) is `otel_libs.run` from `OTEL_RESOURCE_ATTRIBUTES`, else `local`
(design DE-D7 r3.4). The api serves `GET /health` (503 until Redis is connected); the backend serves
`grpc.health.v1.Health` (`""` and `demo.v1.OrderService` are `SERVING` once the
schema is applied).

## Build and test

Images build from the repository root; the helper packages are built and
packed from the workspace source and installed over the published pins of
`package.json` / `package-lock.json`, so an image always runs the helper code
of its commit. After that install the build checks `npm ls --omit=dev`
against the locked tree and fails on any drift. Base images are pinned by
digest:

```bash
docker build --platform linux/amd64 -t otel-libs-demo-node-backend:dev -f nodejs/demo/backend/Dockerfile .
docker build --platform linux/amd64 -t otel-libs-demo-node-api:dev     -f nodejs/demo/api/Dockerfile .
docker build --platform linux/amd64 -t otel-libs-demo-node-worker:dev  -f nodejs/demo/worker/Dockerfile .
```

Unit tests (coverage gate 90 % on `lib/`): `testing/verify.sh --only demo-node`.

`nodejs/demo` is its own npm package with its own lockfile, **not** a member
of the `nodejs/` workspace: the workspace's lint, typecheck, pack, version
and publish checks cover the published packages only.
