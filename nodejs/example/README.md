# Node.js examples

Runnable apps showing `@staffops/otel-helper` in use: a notification service,
the Node twin of the Python one (`python/example/`). Same routes, ports, env
vars, custom span names, span attributes and metric names (instrumentation span
names differ per language: see the end of this page).

| Example | Shows |
|---|---|
| [`node-custom-signals/`](#custom-signals) | custom spans, metrics and logs correlated with the trace — a short job that exits; same names and trace shape as the Go and Python twins |
| `node-api/`, `node-backend/`, `node-process/` | a notification service: express → gRPC distributed tracing, workers with root spans, health probes, graceful shutdown |

Per-endpoint telemetry, Grafana queries and the differences from the Python
and Go examples: [EXAMPLES.md](EXAMPLES.md).

Prerequisites: Docker. Commands run from the repository root.

## Custom signals

[`node-custom-signals/custom-signals.js`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/example/node-custom-signals/custom-signals.js)
processes a batch of three simulated orders (`ord-3` fails) and exits, as
`go-custom-signals` and `python-custom-signals`: a root span
(`startRootSpan`) with one child span per order (attributes, an event, an
error recorded with its status), a counter, a histogram with second-based
buckets, an up-down counter and an observable gauge, and `createLogger()`
records correlated with the active span. What it emits, signal by signal:
[EXAMPLES.md](EXAMPLES.md#custom-signals-node-custom-signals).

```bash
docker build -t node-custom-signals -f nodejs/example/node-custom-signals/Dockerfile nodejs

# Against the repo's test Collector (testing/docker-compose.yml, debug exporter
# with verbosity: detailed, which prints everything it receives)
docker compose -f testing/docker-compose.yml --profile e2e up -d collector
docker run --rm --network otel-libs-testing_default \
  -e OTEL_SERVICE_NAME=node-custom-signals \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  node-custom-signals

# The Collector log keeps every earlier run: filter on the printed trace_id
# (or recreate the Collector first with `up -d --force-recreate collector`).
docker compose -f testing/docker-compose.yml --profile e2e logs --no-log-prefix --tail 2000 collector \
  | grep -B 4 -A 30 <trace_id>
docker compose -f testing/docker-compose.yml --profile e2e stop collector

# Without a Collector: JSON log lines on stdout, no traces
docker run --rm -e OTEL_SERVICE_NAME=node-custom-signals node-custom-signals
```

The job prints `trace_id=<id>` first (filter the Collector log on it) and
flushes everything through `shutdown(5000)` before it exits. Without a
Collector (recorded 2026-11-03, stack trimmed):

```text
otelhelper: deployment.environment.name is not set; set it in OTEL_RESOURCE_ATTRIBUTES (e.g. deployment.environment.name=PRD); log level defaults to INFO using=absent
trace_id=2a1bca8bf62a7b40b865f0c8c9f4190f
{"time":"2026-11-03T22:07:17.225Z","level":"INFO","msg":"order processed","logger":"node-custom-signals","order.id":"ord-1","order.amount":42.5}
{"time":"2026-11-03T22:07:17.248Z","level":"INFO","msg":"order processed","logger":"node-custom-signals","order.id":"ord-2","order.amount":99.9}
{"time":"2026-11-03T22:07:17.274Z","level":"ERROR","msg":"order failed","logger":"node-custom-signals","order.id":"ord-3","exception":"PaymentDeclinedError: card declined by issuer\n    at charge (…/custom-signals.js:99:25)\n    …"}
{"time":"2026-11-03T22:07:17.274Z","level":"INFO","msg":"batch finished","logger":"node-custom-signals","batch.size":3,"orders.failed":1}
```

The first line is a diagnostic (stderr). The JSON lines are the stdout
fallback: no trace IDs, as Python. Metrics are served on `:9464/metrics` only
while the process runs (`order_processed_total`,
`order_duration_seconds_bucket`, `order_active`, `order_queue_depth`).

## Notification service

```text
node-process (workers)
  ├── queue consumer (30 s)  → POST /notify
  ├── retry worker (60 s)    → POST /notify
  ├── cleanup worker (3 min)
  └── health checker (60 s)  → GET /health
         │ HTTP :8000
         ▼
node-api (express)
  ├── POST /notify                  ├── GET /templates
  ├── GET /notify/:notificationId   ├── POST /templates
  ├── GET /metrics/summary          └── GET /health
         │ gRPC :50051
         ▼
node-backend (gRPC server)
  ├── SendNotification → render + deliver (email/sms/push, 10% failures)
  ├── GetStatus
  ├── RenderTemplate
  └── grpc.health.v1.Health
```

What each app demonstrates:

- **node-api** (`main.js`): `setup()` first, then `require('express')` and
  `require('@grpc/grpc-js')`, so both are instrumented and routes get a full
  `http.route`; the gRPC client is built from `notification.proto` at runtime
  with `@grpc/proto-loader` (no generated stubs) and every call has a 5 s
  deadline. `/health` has no server span but counts in
  `http.server.request.duration`. Errors answer JSON: 422 for an invalid body
  (an explicit `null` included), 400 `{"detail": "Bad Request"}` for malformed JSON, 404 `{"detail": "Not
  Found"}`, 500 `{"detail": "Internal Server Error"}` after a `Request failed`
  log record — the 4xx/5xx detail is the fixed status text, never the error's
  message.
- **node-backend** (`server.js`): gRPC server instrumented by `setup()`,
  custom child spans, a histogram with explicit buckets, the standard
  `grpc.health.v1` service (`grpc-health-check`, not traced; NOT_SERVING as
  soon as shutdown starts); a handler that fails answers gRPC `UNKNOWN`, as an
  uncaught exception in grpcio.
- **node-process** (`workers.js`): one root span per worker cycle
  (`startRootSpan(name, fn, { tracer })`, as Go `StartRootSpan` and Python
  `start_root_span`), `fetch` to the API with a 5 s timeout and the shutdown
  signal, `/health` and `/healthz` on `:8080`. That probe server is a
  `node:http` server, so it is instrumented (no span, counted in
  `http.server.request.duration`); python-process's aiohttp probe server
  is not. It does **not** inject `traceparent` by hand: the helper's `fetch`
  instrumentation injects it and appends the header, so a second copy would
  break the trace.

All three handle `SIGTERM`/`SIGINT` themselves (the helper installs no signal
handler): drain (≤ 4 s, in-flight requests aborted), then `shutdown(5000)`, inside `docker stop`'s default
10 s grace period (a second SIGINT or SIGTERM during that window exits at once,
without the flush: the handlers are `process.once`). They take tracers and meters from the helper (`getTracer`, `getMeter`) and
log with its `createLogger()`.

| Variable | Default | App |
|---|---|---|
| `OTEL_SERVICE_NAME` | unset → the SDK's `unknown_service:…` + warning | all |
| `OTEL_RESOURCE_ATTRIBUTES` | unset → no `deployment.environment.name`, INFO | all |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset → Prometheus `:9464/metrics` + JSON logs on stdout | all |
| `BACKEND_URL` | `node-backend-active:50051` | node-api |
| `API_URL` | `http://node-api-stable:8000` | node-process |
| `PORT` | `8000` (api), `50051` (backend) | node-api, node-backend |
| `HEALTH_PORT` | `8080` | node-process |
| `NOTIFICATION_PROTO` | `./protos/notification.proto` next to the entry file, else `../protos/` (a source checkout) | node-api, node-backend |

### Build

The build context is `nodejs/`:

```bash
docker build -t node-api     -f nodejs/example/node-api/Dockerfile nodejs
docker build -t node-backend -f nodejs/example/node-backend/Dockerfile nodejs
docker build -t node-process -f nodejs/example/node-process/Dockerfile nodejs
```

`Dockerfile` builds the helper from the workspace source and installs the
`npm pack` tarball; `Dockerfile.demo` installs a prebuilt tarball from
`nodejs/tarballs/` instead (the CI artifact). Each example's own dependencies are
pinned by its `package-lock.json`; the helper's dependencies resolve within the
helper's declared ranges at install time, as Python's wheel install. The helper is not listed in the examples'
`package.json` on purpose: the `@staffops` npm scope is not claimed yet, so a
registry name could resolve to someone else's package.

### Run

With the repo's test Collector (see `python/example/README.md`), on its network:

```bash
NET=otel-libs-testing_default

docker run -d --rm --name node-backend --network $NET \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  -e OTEL_SERVICE_NAME=node-backend node-backend
docker run -d --rm --name node-api --network $NET -p 8000:8000 \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  -e OTEL_SERVICE_NAME=node-api -e BACKEND_URL=node-backend:50051 node-api
docker run -d --rm --name node-process --network $NET \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  -e OTEL_SERVICE_NAME=node-process -e API_URL=http://node-api:8000 node-process

curl -X POST http://localhost:8000/notify \
  -H "Content-Type: application/json" \
  -d '{"recipient": "user@example.com", "channel": "email", "template_id": "welcome", "variables": {"name": "User", "product": "MyApp"}}'

docker stop node-process node-api node-backend
```

> **Legacy names — do not copy.** The metric names (`*_total`, `*_seconds` in
> the name, no unit) match the Python and .NET examples because the shared
> dashboards use them. New instruments: dot-separated names, the unit in
> `unit`, seconds for durations, no `_total` suffix.

### Trace of a worker cycle

Recorded against collector-contrib 0.160.0 on 2026-11-03 (one trace id across
the three services; the cycle dispatched three notifications, one shown):

```text
process.queue-consume                                    Internal  node-process
└── process.dispatch-notification                        Internal  node-process
    └── POST                                             Client    node-process
        └── POST /notify                                 Server    node-api
            └── request handler - /notify                Internal  node-api
                └── api.send-notification                Internal  node-api
                    └── grpc.notification.NotificationService/SendNotification  Client  node-api
                        └── grpc.notification.NotificationService/SendNotification  Server  node-backend
                            └── backend.send-notification    Internal  node-backend
                                ├── backend.render-template  Internal  node-backend
                                └── backend.deliver          Internal  node-backend
```

Differences from the Python trace are the instrumentations' own (design
D-JS14): express adds a `request handler - <route>` span, FastAPI adds
`http receive`/`http send` spans; gRPC span names are `grpc.<service>/<method>`
in Node and `/<service>/<method>` in Python; the route template is
`/notify/:notificationId` (express) instead of `/notify/{notification_id}`.
The `Notification sent` log record of node-api carries the trace id and the
span id of `api.send-notification`.
