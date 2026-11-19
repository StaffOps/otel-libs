# Python demo apps

The Python language set of the otel-libs demo — `worker` → `api` → `backend`,
PostgreSQL and a Redis Streams queue — as specified in
[`specs/demo-environment/`](../../specs/demo-environment/) (task DE3b). The
shared contract (proto, schema, connection table) is in [`demo/`](../../demo/README.md).

| App | File | Role |
|---|---|---|
| worker | [`worker/worker.py`](worker/worker.py) | One `POST <DEMO_API_URL>/orders` at a fixed rate of one per `DEMO_INTERVAL_SECONDS` (5; a tick, each request its own task) in a root span `demo.drive`, cycling `ok ok business_error ok ok db_error ok ok unhandled ok` (DE-D7); polls `demo:python:orders` every `DEMO_POLL_SECONDS` (2) with `XREADGROUP COUNT 10` (no `BLOCK`) and processes each event in a CONSUMER span whose parent and link are the producer (DE-D6) |
| api | [`api/api.py`](api/api.py) | FastAPI on `DEMO_HTTP_PORT` (8080), `GET /health`: demo span `demo.order.accept`, Redis quote cache (TTL 30 s), `grpc.aio` call to `PlaceOrder`, PRODUCER span `send demo:python:orders` + `XADD` with `traceparent`/`tracestate`/`baggage` fields; answers 201 / 400 (invalid body) / 422 / 409 / 502 (any other gRPC failure) / 500 (`unhandled`); a cache or publish failure is one WARN and the answer stays 201 |
| backend | [`backend/backend.py`](backend/backend.py) | `grpc.aio` server on `DEMO_GRPC_PORT` (50051) with `grpc.health.v1`: applies `demo/sql/schema.sql` at start (one command, no bind parameters), validates `scenario` (`INVALID_ARGUMENT`), demo span `demo.order.process` (any failure there, expected or not, follows ADR-014; an unexpected one answers `INTERNAL` `order failed`) with the custom metrics `demo.orders.processed` / `demo.order.duration`, SQLAlchemy 2 + psycopg 3 |
| shared | [`common/democommon.py`](common/democommon.py) | Settings, scenario cycle, error types (pinned `error.type` through `__otel_error_type__`), the metric `error.type` value |

Telemetry comes from `staffops-otel-helper` with the `redis` and `sql` extras,
installed from the local tree; `instrument_redis()` / `instrument_sql()` are
called before `setup_telemetry()`. Demo spans and instruments use the scope
`otel-libs-demo`. Every failure follows ADR-014 in its owning span
(`demo.order.process`, `demo.order.accept`), which runs in the helper's
`staffops_otel_helper.traced` (contract design D15, A45): status ERROR without
description, `error.type` (`OrderRejectedError`, `OrderExistsError`,
`DemoUnhandledError`, from each class's `__otel_error_type__`), no `exception`
event, and one ERROR record on the logger `staffops_otel_helper.failure` in that
span's context, with `exception.message` and `exception.stacktrace`. Spans that
only propagate a failure (`demo.order.accept` on 422/409/502, `demo.drive`) are
marked by hand and write no failure record.

## Configuration

Environment only (DR0, DE-D13). Passwords are read once from files and never
logged:

| Variable | App | Default |
|---|---|---|
| `DEMO_API_URL` | worker | `http://demo-python-api:8080` |
| `DEMO_INTERVAL_SECONDS`, `DEMO_POLL_SECONDS` | worker | `5`, `2` |
| `DEMO_HTTP_PORT` | api | `8080` |
| `DEMO_BACKEND_ADDR` | api | `demo-python-backend:50051` |
| `DEMO_REDIS_ADDR`, `DEMO_REDIS_PASSWORD_FILE` | worker, api | `demo-redis:6379`, — (required) |
| `DEMO_GRPC_PORT` | backend | `50051` |
| `DEMO_PG_HOST`, `DEMO_PG_DB`, `DEMO_PG_USER`, `DEMO_PG_PASSWORD_FILE` | backend | `demo-postgres` (`host[:port]`), `demo_python`, `demo_python`, — (required) |
| `DEMO_SCHEMA_FILE` | backend | `/etc/otel-libs-demo/schema.sql` (baked into the image) |

`seq` starts at 1; order ids are `ok-<seq mod 500>` and `ord-<seq mod 500>`;
customers `cust-a`, `cust-b`, `cust-c`; amounts `10 + seq mod 90`, except
`business_error`, which sends 5000 (the backend rejects any amount over 1000).
`db_error` is a plain `INSERT` of `dup-<run>`. The consumer group is created at
`$` with `MKSTREAM` after an existence check (no failing command on a normal
start); the worker recreates it after a `NOGROUP` reply. Invalid numeric
settings fall back to the default with one warning (DE-D7 r3.4/r3.5). On a
4xx the api's `demo.order.accept` is ERROR with no description, `error.type`,
event or log. The run id of the run tokens (`rej-<run>`, `dup-<run>`, `unh-<run>`) is read
from `otel_libs.run` in `OTEL_RESOURCE_ATTRIBUTES` (`local` when absent); the
apps set no resource attribute themselves.

## Build and test (Docker only)

```bash
# Images, from the repository root (each Dockerfile has a Dockerfile.dockerignore allow-list)
docker build --platform linux/amd64 -f python/demo/backend/Dockerfile -t otel-libs-demo-python-backend:dev .
docker build --platform linux/amd64 -f python/demo/api/Dockerfile     -t otel-libs-demo-python-api:dev .
docker build --platform linux/amd64 -f python/demo/worker/Dockerfile  -t otel-libs-demo-python-worker:dev .

# Unit tests, coverage gate 90 % (also: testing/verify.sh --only demo-python)
python/demo/run-tests.sh

# Regenerate the hashed lock after changing requirements.in
python/demo/lock.sh
```

Images: `python:3.11-slim` by digest (the tests run on the same digest), a venv from
[`requirements.lock`](requirements.lock) (`--require-hashes --no-deps`), the
helper from `python/` with `--no-deps --no-build-isolation` (its build backend
`hatchling` is in the lock and removed afterwards), stubs generated from the shared proto
at build time (`grpcio-tools` is removed from the runtime image), bytecode
compiled at build (read-only root filesystem works), user `65532`.
