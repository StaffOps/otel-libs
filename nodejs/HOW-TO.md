# HOW-TO: Using @staffops/otel-helper (Node.js)

Practical guide for Node.js developers instrumenting a service with
`@staffops/otel-helper` 0.3. The variable reference is in
[README.md](README.md#environment-variables); runnable examples are in
[example/](example/README.md).

Prerequisites: Node.js 22.15.0+, the helper installed (not released yet — see
[README, Installation](README.md#installation)), and an OpenTelemetry
Collector reachable over OTLP (or none, for the Prometheus fallback).

- [1. Getting started](#1-getting-started)
- [2. HTTP servers, express and outbound HTTP](#2-http-servers-express-and-outbound-http)
- [3. gRPC](#3-grpc)
- [4. Workers and background jobs](#4-workers-and-background-jobs)
- [5. Logs](#5-logs)
- [6. Custom signals: spans, metrics, logs](#6-custom-signals-spans-metrics-logs)
- [7. Sampling](#7-sampling)
- [8. Debug mode](#8-debug-mode)
- [9. Opt-in instrumentation packages](#9-opt-in-instrumentation-packages)
- [10. Metrics without a Collector (Prometheus `/metrics`)](#10-metrics-without-a-collector-prometheus-metrics)
- [11. OTLP endpoint, protocol, TLS and exporter options](#11-otlp-endpoint-protocol-tls-and-exporter-options)
- [12. `SetupOptions` and `Resolved` reference](#12-setupoptions-and-resolved-reference)
- [13. Lifecycle: setup, shutdown, SIGTERM, cluster, worker threads](#13-lifecycle-setup-shutdown-sigterm-cluster-worker-threads)
- [14. Validation and diagnostics](#14-validation-and-diagnostics)
- [15. Testing your app](#15-testing-your-app)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

---

## 1. Getting started

The helper patches libraries **as they load**, so it must run before your app
loads `express`, `@grpc/grpc-js` or the libraries of the opt-in packages. Pick
one of three ways.

**A. Preload from the environment** (no code change):

```bash
export OTEL_SERVICE_NAME=orders-api
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

node --import @staffops/otel-helper/register server.mjs
```

`@staffops/otel-helper/register` installs the module hooks (ES modules and
CommonJS), then calls `setup()` with no options. `--import` and `--require`
both work for ES module and CommonJS apps (checked on Node 24 with an express
ES module app: route-named spans either way). Both also run in every
file-based worker thread; only `--require` runs in an `eval` worker
([§13](#worker-threads)). A failed `setup()` from `/register` is
reported on stderr, never thrown: a preload must not crash the app.

**B. CommonJS: `setup()` first in the entry file:**

```js
// server.js
const { setup } = require('@staffops/otel-helper');

setup(); // before anything it instruments is required

const express = require('express');
const routes = require('./routes'); // routers built after setup(): full http.route
```

**C. ES modules or code options: a preload file of your own.** `import`
statements are hoisted, so a `setup()` call in an ES module entry runs after
every import of that file — too late for `express`. Put it in a preload:

```js
// otel.mjs
import '@staffops/otel-helper/hooks';           // first: the module hooks
import { isMainThread } from 'node:worker_threads';
import { setup } from '@staffops/otel-helper';
import { sqlInstrumentation } from '@staffops/otel-helper-sql';

// A preload also runs in every worker thread: set up the main thread only.
if (isMainThread) setup({ serviceName: 'orders-api', instrumentations: [sqlInstrumentation()] });
```

```bash
node --import ./otel.mjs server.mjs
```

What runs before `setup()` matters:

- **Modules loaded before `setup()`** — the helper re-patches `express` and
  `@grpc/grpc-js` it finds in `require.cache` and reports
  `<module> was loaded before setup(); …`, but apps, routers and clients built
  before it may lack spans or have an incomplete `http.route`
  ([§2](#routes-modules-after-setup)).
- **An ES module process without the hooks** — `setup()` warns once:
  `setup() runs in an ES module process without the module hooks; packages imported after setup() (…) are not instrumented`.
  The check recognizes an ES module entry by `--input-type=module`, an
  `.mjs`/`.mts` file, or a `.js`/`.ts`/extensionless file in a
  `"type": "module"` package. A `.js` file that Node runs as ESM only by
  **syntax detection** (no `type` field), or a CommonJS entry that `import()`s
  ES modules later, gets no warning — use `--import` anyway.
- **Meters obtained before the first `setup()`** are permanent no-ops (the
  metrics API has no proxy): create instruments after `setup()`. Tracers and
  OpenTelemetry Logs API loggers obtained earlier join the helper once it is
  installed.

Call `setup()` once per process, at startup. In a `cluster` or pm2 app, call
it in every worker ([§13](#cluster-and-pm2)).

---

## 2. HTTP servers, express and outbound HTTP

`setup()` instruments `node:http`/`node:https` servers and clients, `fetch`
(undici) and express. Nothing to wrap:

```js
const { setup } = require('@staffops/otel-helper');
setup();

const express = require('express');
const app = express();
app.get('/orders/:id', (req, res) => res.json({ id: req.params.id }));
app.listen(8000);
```

An express request gives one SERVER span named `GET /orders/:id` and one
INTERNAL `request handler - /orders/:id` span per matched handler.

### Routes modules after `setup()`

express wires its route layers when a router is built. A router or app
created **before** `setup()` gets a truncated `http.route` (`GET /api` for
`/api/orders/:id`) or none, and may miss request-handler spans. Require the
modules that build apps and routers (and gRPC services and clients) after
`setup()`, or use a preload (§1 A or C).

### What is and is not instrumented

- `http.route` exists only with express. A plain `node:http` server has no
  route: its span is named `GET` and its `http.server.request.duration` point
  has no `http.route`.
- **`node:http2` servers and clients (h2c included) are not instrumented**:
  there is no official OpenTelemetry HTTP/2 instrumentation. Go `otelhttp` and
  Python ASGI see HTTP/2 requests; in Node.js, serve HTTP/2 behind a proxy that
  speaks HTTP/1.1 to the app, or add spans yourself.
- Fastify is not instrumented until `@fastify/otel` supports SDK 3.x.
- Two HTTP metrics only: `http.server.request.duration` and
  `http.client.request.duration` (no body sizes, no
  `http.server.active_requests`).
- `error.type` on a transport error is the JS error name (`Error`,
  `TypeError`).

### Health checks

Server spans for `/health /healthz /ready /readyz /live /livez /ping /metrics`
are dropped (exact match, case-insensitive, trailing slash and query
ignored); the requests still count in `http.server.request.duration`, so
exclude them in SLI queries. Replace the list in code:

```js
setup({ healthPaths: ['/status', '/k8s/ready'] }); // [] traces every path
```

Each path must be ASCII, start with `/` and carry no query, fragment or
comma.

### Outbound HTTP and propagation

`fetch` and `http.request` calls get CLIENT spans and the `traceparent`
header automatically.

> **Never call `propagation.inject()` into the headers of an instrumented
> `fetch`.** The undici instrumentation appends its own `traceparent`, so the
> request carries the header twice and the receiving service starts a new
> trace. The examples inject nothing by hand.

```js
// Right: the instrumentation propagates the context
const res = await fetch(`${API_URL}/notify`, { method: 'POST', body, headers: { 'content-type': 'application/json' } });
```

### URLs: what is redacted

Client spans write `url.full`, server spans `url.path` and `url.query`:

- userinfo in a `node:http` client URL is written as `REDACTED:REDACTED`
  (`fetch` rejects URLs with credentials);
- the values of `sig`, `Signature`, `AWSAccessKeyId`, `X-Goog-Signature`,
  `X-Amz-Signature`, `X-Amz-Credential` and `X-Amz-Security-Token` are written
  as `REDACTED` in client `url.full` and server `url.query`, as in Go and
  Python. Names are matched case-sensitively (`SIG` is kept);
- every other value (`token`, `access_token`) is recorded as sent.

Do not put secrets in query strings. When a listed name is present, the
recorded query is re-serialized through `URLSearchParams` for `node:http`
clients (`url.full`: duplicates collapsed, names decoded, the fragment
dropped) and for servers (`url.query`: also `;` and `=` encoded); a `fetch`
client keeps the query as written apart from the redacted values. Go and
Python rewrite only the listed values. The request itself is never changed.

---

## 3. gRPC

`@grpc/grpc-js` clients and servers are instrumented by `setup()`; the
`traceparent` travels in the call metadata. Load `@grpc/grpc-js` (and build
services and clients) after `setup()`.

```js
const { setup } = require('@staffops/otel-helper');
setup();

const grpc = require('@grpc/grpc-js');
const server = new grpc.Server();
server.addService(service, {
  sendNotification(call, callback) {
    doWork(call.request)
      .then((reply) => callback(null, reply))
      .catch((err) => callback({ code: grpc.status.INTERNAL, message: 'delivery failed' }));
  },
});
```

- **Return errors through the callback (or a rejected promise); never throw
  synchronously in a handler.** A handler that throws leaves its SERVER span
  unended, so it is never exported (upstream `@opentelemetry/instrumentation-grpc`);
  Go and Python record the call as `UNKNOWN`.
- Spans are named `grpc.<package.Service>/<Method>` and carry the legacy
  `rpc.system`, `rpc.service`, `rpc.method`, `rpc.grpc.status_code`; error
  spans add `grpc.error_name` / `grpc.error_message` and no `error.type`. No
  RPC metrics are emitted. Go and Python name the spans differently (README,
  known gaps): a query on gRPC span names is per language.
- A SERVER span is ERROR only for the server-fault codes (as the semconv gRPC
  rule): `UNKNOWN`, `DEADLINE_EXCEEDED`, `UNIMPLEMENTED`, `INTERNAL`,
  `UNAVAILABLE`, `DATA_LOSS`. The rule also clears an `ERROR` status your
  handler set on a SERVER span whose code is not one of those.
- `grpc.health.v1.Health` calls (Check, Watch, List) produce no span.

---

## 4. Workers and background jobs

A worker cycle, a queue message or a cron run is its own trace. Use
`startRootSpan()`: it starts a new trace whatever span is active, makes it the
active span inside `fn`, and ends it when `fn` returns or its promise settles.

```js
const { getTracer, setup, startRootSpan } = require('@staffops/otel-helper');
setup();

const tracer = getTracer('notification-worker');

async function cycle() {
  await startRootSpan('process.queue-consume', async (span) => {
    const batch = await fetchBatch();
    span.setAttribute('batch.size', batch.length);
    for (const item of batch) await dispatch(item); // HTTP/gRPC calls are children
  }, { tracer, attributes: { 'queue.name': 'notifications' } });
}
```

- Attributes passed in `attributes` are set at span start, so the sampler
  sees them. Baggage and other context values are kept; the sampler sees no
  parent, so the root is sampled on its own.
- A throw or rejection records an exception event and status ERROR
  `"<error name>: <message>"`, then rethrows the original value.
  `exception.type` is the error's `name` (set `this.name` in your error
  classes, or it reads `Error`).
- **A promise that never settles leaves the span open: it is never exported**
  (as an unclosed span in Go or Python). Give every awaited call a timeout
  (`AbortSignal.timeout()`, a gRPC deadline).
- Work `fn` starts **without awaiting it** runs under the span after it
  ended: its child spans keep the trace, but the root no longer covers them.
  Await everything that belongs to the cycle.

### Short-lived jobs and scripts

A process that **ends on its own** (the event loop empties) is flushed by the
helper: the first successful `setup()` registers one
`process.once('beforeExit')` listener that calls `shutdown()` when the app has
not (as Python's SDK does at interpreter exit). `beforeExit` is **not**
emitted on `process.exit()` or when a signal kills the process, so:

```js
await runJob();
await shutdown();       // before process.exit(), always
process.exit(code);
```

Letting the job return without `process.exit()` works too; a `SIGTERM` still
needs the handler of [§13](#shutdown-and-sigterm).

- **The automatic flush can delay the exit.** With an unreachable or
  blackholed Collector, each signal's export waits up to
  `OTEL_EXPORTER_OTLP_TIMEOUT` (10 s by default; 10 s measured), all bounded by
  the 30 s default deadline of `shutdown()`. To bound it, call
  `await shutdown(5000)` yourself at the end; the listener then does nothing.
  There is no opt-out of the listener.
- **Do not emit telemetry from your own `beforeExit` listener**, or from work
  it schedules: the helper's flush may already have run (listeners run in
  registration order, and the helper's was registered at `setup()`), so it is
  dropped. Call `await shutdown()` at the end of your own code instead.
- **Worker threads are not flushed when they end** (the listener exists in the
  main thread only): a worker that called `setup()` must
  `await shutdown()` before its last task returns.

---

## 5. Logs

```js
const { createLogger } = require('@staffops/otel-helper');

const logger = createLogger({ name: 'orders' }); // the stdout `logger` field and the OTel scope
logger.info('order processed', { 'order.id': 'ord-1', 'order.amount': 42.5 });
try {
  await charge(order);
} catch (err) {
  logger.error('order failed', { 'order.id': 'ord-3', error: err }); // the Error becomes the record exception
}
```

A logger can be created before `setup()`: its destination and default level
are read per record. Its methods never throw.

### Where records go

| Situation | Destination |
|---|---|
| A logs exporter is active (`otlp` with a logs endpoint, or `console`) | OpenTelemetry Logs API, with the active span's trace and span ids |
| No logs endpoint, `OTEL_LOGS_EXPORTER=none`, before `setup()`, after a failed one, or under another `@opentelemetry/api` version's registry | One JSON line on stdout |

Never both. The stdout line has the fields of the Go and Python fallback —
`time` (UTC, `Z`), `level`, `msg`, `logger`, your attributes, `exception` —
and no trace ids (as Python):

```json
{"time":"2026-11-03T22:41:15.525Z","level":"ERROR","msg":"order failed","logger":"orders","order.id":"ord-3","exception":"Error: boom\n    at …"}
```

`OTEL_LOGS_EXPORTER=console` is a **debugging format**: the SDK's console
exporter prints a multi-line `console.dir` dump of each record, which a log
pipeline cannot parse line by line. To ship logs from stdout, use the JSON
fallback (no logs exporter, or `OTEL_LOGS_EXPORTER=none`).

### Attributes and errors

- The first `Error`-valued attribute becomes the record's exception
  (`exception.type`, `exception.message`, `exception.stacktrace`) and leaves
  the attributes. `exception.type` is the error's `code` when it has one
  (`ECONNREFUSED`), else its `name`.
- On the OTel path plain objects are OTLP maps and arrays OTLP arrays; on
  stdout they are JSON. `Date` is ISO 8601 text, `bigint` text, a cycle
  `[Circular]`, nesting deeper than 100 `<max-depth-exceeded>`.

### Log level

The default level comes from the environment: `LOCAL` debug, `DEV`/`HML`
info, `PRD` warn, unset or anything else info; `OTEL_HELPER_DEBUG_LEVEL=true`
gives debug. Override it per logger (`createLogger({ name, level: 'warn' })`)
or for the process (`setup({ minimumLogLevel: 'debug' })`).
`defaultLogLevel(env, debug)` returns the same rule. The SDK's own messages
follow `OTEL_LOG_LEVEL` (default WARN), not this level.

### pino

```js
const pino = require('pino');
const { createPinoDestination } = require('@staffops/otel-helper-pino');

const log = pino({ name: 'checkout' }, createPinoDestination()); // or pino(createPinoDestination())
log.info({ order: 'o1' }, 'order received');
```

- Pass the destination explicitly: the helper never rewrites your
  destinations. **Do not** put it in a `pino.multistream` next to
  `process.stdout` — before `setup()` (or without a logs exporter) both write
  stdout, so each record appears twice.
- `pino.transport({ target: '@staffops/otel-helper-pino' })` is **not
  supported**: a transport runs in a worker thread with no helper setup; it
  fails with a clear error.
- `trace`-level records are always dropped, and the helper level applies on
  top of pino's own (`createPinoDestination({ minimumLogLevel: 'warn' })`).
- pino's `time` is not a field (the record time is the write time); `pid` and
  `hostname` are left out only when they are pino's default `base`. Keys
  produced by `formatters.level` (other than `level`) or a custom `timestamp`
  stay fields.
- `levelComparison: 'DESC'` is not supported (levels are bucketed by pino's
  ascending numbers).
- A direct non-JSON write with newlines becomes one record per line.
- `logger.flush(cb)` flushes the OTLP records too: call it before
  `process.exit()`.

### winston

```js
const winston = require('winston');
const { OtelHelperTransport } = require('@staffops/otel-helper-winston');

const log = winston.createLogger({
  format: winston.format.errors({ stack: true }), // logger level: the Error is kept as the exception
  transports: [new OtelHelperTransport({ name: 'checkout' })],
});
log.info('order received', { order: 'o2' });
```

- Add the transport explicitly; the helper never rewrites your transports.
- Correlation and time come from the logging call, also for records winston
  buffers under backpressure.
- A logger-level `format.errors({ stack: true })` is rebuilt as the record
  exception. A **transport-level** format loses `logger.error(err)`'s message
  and stack (winston copies own keys only): keep `format.errors()` on the
  logger.
- Records written **before** the transport was attached (`logger.add()`
  later) and still buffered arrive uncorrelated (root context).
- winston's `'logged'` event fires also for records the helper level drops.
- Level names map by the anchors of the logger's own level set: npm
  `verbose` is debug, cli `verbose` trace (always dropped).
- `logger.end()` flushes the OTLP records: call it before `process.exit()`.

### Overhead of the log bridges

Measured 2026-11-03 in Docker (`node:24-slim`, stdout to `/dev/null`, 50 000
records, the stdout path):

| Library | To a no-op destination | Through the helper | A record the helper level drops |
|---|---|---|---|
| pino | 2.9 µs/record | 5.3 µs/record | 1.2 µs/record |
| winston | 2.2 µs/record | 9.8 µs/record | — |

---

## 6. Custom signals: spans, metrics, logs

The complete runnable version is
[`node-custom-signals`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/example/node-custom-signals/custom-signals.js)
([EXAMPLES.md](example/EXAMPLES.md#custom-signals-node-custom-signals));
the same names and trace shape as the Go and Python twins.

### Spans

```js
const { SpanStatusCode } = require('@opentelemetry/api');
const { getTracer } = require('@staffops/otel-helper');

const tracer = getTracer('orders'); // instrumentation scope: your module

function processOrder(order) {
  return tracer.startActiveSpan('order.process', { attributes: { 'order.id': order.id } }, async (span) => {
    try {
      span.addEvent('order.validated', { 'order.amount': order.amount });
      await charge(order);
    } catch (err) {
      span.recordException(err);
      span.setStatus({ code: SpanStatusCode.ERROR, message: 'payment declined' });
      throw err;
    } finally {
      span.end(); // startActiveSpan does not end the span for you
    }
  });
}
```

`getTracer(name?, version?)` reads the global provider; the default scope is
`otel-helper` (as Go and Python). The service name comes from the resource,
not from the tracer name. Note: `span.recordException(err)` takes
`exception.type` from the error's `code` when it has one; `startRootSpan()`
always writes the `name`.

### Metrics

```js
const { ValueType } = require('@opentelemetry/api');
const { getMeter } = require('@staffops/otel-helper');

const meter = getMeter('orders');
const processed = meter.createCounter('order.processed', {
  valueType: ValueType.INT, // integer points, as Go Int64 and Python int
  unit: '{order}',
  description: 'Orders processed',
});
const duration = meter.createHistogram('order.duration', {
  unit: 's',
  advice: { explicitBucketBoundaries: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1] },
});

processed.add(1, { 'order.type': 'standard' });
duration.record(0.042, { 'order.type': 'standard' });
```

- **Pass `valueType: ValueType.INT` on counts.** The JS API defaults to
  doubles, so a counter, up-down counter or gauge without it exports `1` as
  `Double(1)`; Go and Python export integers. The Prometheus names are the
  same either way.
- Dot-separated names, the unit in `unit` (`s` for durations), no `_total`
  suffix (the Prometheus exporter adds it). A second-based histogram needs its
  own boundaries: the SDK defaults suit milliseconds.
- Metric attributes must be low-cardinality: `order.type`, `error.type` —
  never `order.id` or a user id (put those on spans).
- Create instruments after `setup()` (§1). Observable callbacks registered on
  a meter from the global provider follow every re-setup until you remove
  them (`removeCallback`).

### Logs correlated with the trace

`createLogger()` records carry the trace and span ids of the span active when
they are emitted (OTel path only; see [§5](#5-logs)). Log inside the span:

```js
tracer.startActiveSpan('order.process', (span) => {
  logger.info('order processed', { 'order.id': order.id }); // trace_id/span_id of order.process
  span.end();
});
```

### Personal data

Span and log attributes are exported as written. The examples use synthetic
values (`notification.recipient`); a real e-mail address, phone number or
customer name is personal data and stays out of telemetry — record an
internal id instead.

---

## 7. Sampling

The default is `parentbased_always_on`: a root span is sampled, a child
follows its parent's decision. Tail sampling at the Collector decides what is
kept.

Head sampling, if you need it:

```bash
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.25
```

or `setup({ sampleRatio: 0.25 })` (`ParentBased(TraceIdRatioBased(0.25))`,
wins over the variables). **With services in several languages, use
`parentbased_traceidratio`, never a bare `traceidratio`**: the SDKs derive the
ratio decision from the trace id differently, so bare ratio decisions are not
consistent across a trace, while a parent-based sampler follows the root's
decision. `OTEL_HELPER_SAMPLE_RATIO` is deprecated (same effect, warned).

---

## 8. Debug mode

`OTEL_HELPER_DEBUG_LEVEL=true` (or `setup({ debug: true })`) sets the helper
log level to debug and adds `debug="true"` to the service's **entry spans**
(no parent, or a remote parent), so a Collector tail-sampling policy can keep
those traces. The Collector policy needs an environment allow-list and a rate
limit (root README, Collector prerequisites). Do not leave it on in
production: it marks every inbound request.

---

## 9. Opt-in instrumentation packages

Databases, Redis and the AWS SDK are separate packages. Each exports a
factory whose result goes in `setup({ instrumentations })`:

```js
const { setup } = require('@staffops/otel-helper');
const { sqlInstrumentation } = require('@staffops/otel-helper-sql');
const { redisInstrumentation } = require('@staffops/otel-helper-redis');
const { awsInstrumentation } = require('@staffops/otel-helper-aws');

setup({ instrumentations: [sqlInstrumentation(), redisInstrumentation(), awsInstrumentation()] });

const pg = require('pg'); // after setup()
```

Rules shared by the three:

- **One factory result per process and per `setup()`.** Every result of a
  factory shares the same contrib instrumentation; pass the same one to a
  re-setup.
- **Create it, and call `setup()`, before loading the library.** A library
  loaded earlier is still patched on the first enable (the helper re-requires
  its cached files), but load order is the reliable path.
- Instrumentations are matched by name: passing a raw contrib instrumentation
  (`PgInstrumentation`, `IORedisInstrumentation`, `AwsInstrumentation`) next
  to the factory, or one the core already installs
  (`@opentelemetry/instrumentation-{http,undici,express,grpc}`), makes
  `setup()` throw a `TypeError` — it would patch twice and lose what the
  package masks.
- Not enabled under `OTEL_SDK_DISABLED=true` or under another
  `@opentelemetry/api` version's registry; `shutdown()` disables them.
- A failed DB or AWS call keeps status ERROR **without a description** (the
  server text can quote values), with `error.type`. The semconv `exception`
  event differs per library: pg records a sanitized message
  (`PostgreSQL error of type '<name>' occurred (code: <SQLSTATE>)`), mysql2
  records no event, Redis and AWS keep the server's error text (the
  contract's accepted residual).
- `OTEL_SEMCONV_STABILITY_OPT_IN` is read once per process, when the first
  factory (`sqlInstrumentation()`, `redisInstrumentation()` or
  `awsInstrumentation()`) is **called**: `db.system.name` is always written,
  and the legacy `db.system` too unless the value holds `database` without
  `database/dup`.
- **ES module apps.** A CommonJS library is instrumented without the loader
  hooks only when the file its instrumentation patches is loaded by
  `require()` inside the package: `pg` (`lib/client.js`), node-redis
  (`@redis/client`) and the AWS SDK v3 clients work; **ioredis needs the
  hooks** (its patched file is the package entry). Use `--import` (§1) and the
  question does not arise.
- `OTEL_HELPER_EXTRA_INSTRUMENTATION` has no effect in Node.js (warned once).

### SQL (`@staffops/otel-helper-sql`)

```js
sqlInstrumentation();                              // pg and mysql2, whichever the app loads
sqlInstrumentation({ pg: true, mysql2: false });   // true: warn once when pg does not resolve
```

- A failed query's span gets `error.type=_OTHER` (pg and mysql2 give the
  wrapper no error code); the SQLSTATE is only on pg's
  `db.client.operation.duration` as `error.type`, and only for the callback
  form (`pool.query(…, cb)`), not for a rejected `client.query()` promise.
- Spans carry `db.system.name` (+ legacy `db.system`), `db.namespace`,
  `server.address`, `server.port` and `db.query.text` **as written**
  (placeholders kept, never the bound values; a literal you inline is kept).
  No connection string, user or password.
- Span names: pg `pg.query:<OPERATION> <database>`, `pg.connect`,
  `pg-pool.connect`; mysql2 the statement's first word (`SELECT`).
- Metrics: pg `db.client.operation.duration` and the pool instruments
  `db.client.connection.count` (`state` `used`/`idle`),
  `db.client.connection.max`, `db.client.connection.pending_requests`,
  labelled by `db.client.connection.pool.name` (`host:port/database`). A pool
  built from a **`connectionString`** is named
  `unknown_host:unknown_port/unknown_database`, so two such pools merge into
  one series: pass `host`/`port`/`database` options if you run several
  pools. mysql2 records no metric.
- mysql2 `connection.query(sql, values)` **without a callback** (the
  event-emitter form with values) produces a span that is never ended:
  use the promise API or a callback.
- Installing the package also installs `@types/pg` and `@types/pg-pool`
  (runtime dependencies of `@opentelemetry/instrumentation-pg`).

### Redis (`@staffops/otel-helper-redis`)

```js
redisInstrumentation();                              // node-redis and ioredis
redisInstrumentation({ ioredis: true, redis: false });
```

- Spans are named by the command, uppercased (`SET`; connection spans
  `connect`; as Python — Go writes `set`), with `db.system.name=redis`, `db.operation.name`,
  `server.address`/`server.port` and `db.query.text` as the command and one
  `?` per argument (`SET ? ?`): **no keys, values or `AUTH` passwords**.
- MULTI and pipelines: one span per command.
- `error.type` and `db.response.status_code` are the Redis reply code
  (`WRONGTYPE`, `ERR`, `NOAUTH`); without one (`connect ECONNREFUSED`,
  `Connection is closed.`) `error.type` is `_OTHER` and
  `db.response.status_code` is absent. The exception event
  can still quote arguments (`ERR unknown command 'X', with args beginning with: '<arg>'`).
- No Redis metrics in Node.js (Go emits pool metrics).
- **ioredis connection handshakes are root traces.** ioredis traces `connect`
  and its handshake commands (`HELLO`, `CLIENT`, `INFO`, plus `AUTH`/`SELECT`
  when configured; Cluster `CLUSTER SLOTS` refreshes, Sentinel queries) on
  every connect and reconnect, each as its own trace. While the server is
  down, each retry emits one OK `connect` root; the handshake roots are ERROR
  when the server rejects the auth or is loading. Mitigations: wrap the
  client's lifetime in a span of your own, or drop those root spans at the
  Collector (below).

A Collector filter for the ioredis handshake roots — **not part of any
shipped Collector configuration**. It matches by name on root spans, so it
also drops node-redis's root `connect` span and your own root-level `INFO`,
`CLIENT`, `SELECT` … calls made outside a request:

```yaml
processors:
  filter/ioredis-handshake:
    error_mode: ignore
    traces:
      span:
        - >-
          IsRootSpan()
          and attributes["db.system.name"] == "redis"
          and IsMatch(name, "^(connect|HELLO|CLIENT|INFO|AUTH|SELECT|READONLY|CLUSTER|SENTINEL)$")
```

Put it before tail sampling in the traces pipeline (checked 2026-11-03 with
collector-contrib 0.160.0: root `connect` and `INFO` dropped, a child `INFO`
and a root `SET` kept). `db.system.name` is on every Redis span, whatever
`OTEL_SEMCONV_STABILITY_OPT_IN` says; check the names your clients emit
first.

### AWS SDK v3 (`@staffops/otel-helper-aws`)

```js
awsInstrumentation(); // no options yet: any key throws a TypeError

const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3');
```

- One span per `send()`, retries included, named `<Service>.<Operation>`
  (SQS `<queue> send`/`receive`, SNS `<topic> send`, Lambda
  `<function> Invoke`), with the legacy `rpc.system=aws-api`, `rpc.service`,
  `rpc.method` (as Python; Go writes the current keys), `cloud.region`,
  `aws.region`, `aws.request_id` and the service attributes. No
  `server.address` on Node.js AWS spans.
- **No HTTP span under it**, and the SDK's own requests are not counted in
  `http.client.request.duration`.
- Never recorded: credentials, request and response bodies, DynamoDB
  `item_collection_metrics`, an SNS phone number (`phone_number:**` in the
  attributes and span name). An SNS `InvalidParameter` exception event still
  quotes the number (the exception-message residual).
- `error.type` is the AWS error code (`NoSuchKey`, `ThrottlingException`);
  a body-less service error (an S3 `HEAD` 403) gives the status name
  (`Forbidden`), a transport error its Node `code` (`ECONNREFUSED`) or `Error`,
  anything else `_OTHER`. The status has no description. A Lambda function error
  (`FunctionError: Unhandled`) is a successful AWS call: not an ERROR span.
- A client built before `setup()` is instrumented. ES module apps need no
  loader hooks for the AWS SDK.
- **A streamed S3 `Body` runs its listeners in the SDK's suppressed HTTP
  context**: a traced call made there records nothing. Save the context first:

```js
const { context } = require('@opentelemetry/api');

const { Body } = await s3.send(new GetObjectCommand({ Bucket, Key }));
const saved = context.active();
Body.on('end', () => context.with(saved, () => notifyDone())); // notifyDone()'s fetch is traced
```

- Presigning (`getSignedUrl` of `@aws-sdk/s3-request-presigner`) records a
  CLIENT span (`S3.GetObject`) with no request behind it. Drop it at the
  Collector if it matters.

---

## 10. Metrics without a Collector (Prometheus `/metrics`)

Without a metrics endpoint the helper serves `GET`/`HEAD /metrics` on port
9464 (dual-stack `::`; `OTEL_HELPER_METRICS_PORT`). The listener does not
keep the process alive.

### Choosing exporters

| `OTEL_METRICS_EXPORTER` (or `metricExporters`) | Result |
|---|---|
| unset, no metrics endpoint | `prometheus` |
| unset, metrics endpoint set | `otlp` |
| `otlp,prometheus` | both |
| `console` | the SDK console exporter (debugging) |
| `none` | no metrics |

An env `otlp` without an endpoint warns and falls back; a code `otlp` without
one throws.

### Serving `/metrics` from your own server

```js
const { metricsHandler, setup } = require('@staffops/otel-helper');
setup({ prometheusMetricsPort: 0 }); // or OTEL_HELPER_METRICS_PORT=0: no helper listener

app.get('/metrics', metricsHandler()); // express; also (req, res) of node:http
```

| State | `metricsHandler()` answers |
|---|---|
| Before `setup()`, after a failed one, after `shutdown()`, or the global MeterProvider is not the helper's | 503 |
| Prometheus deliberately off (`OTEL_SDK_DISABLED=true`, or the exporters exclude `prometheus`) | 200, empty body |
| Otherwise | 200, exposition format 0.0.4 |

A busy port does not fail `setup()`: the helper reports
`the /metrics listener could not bind port <n>; …` and the mounted handler
keeps working. Under `cluster` see [§13](#cluster-and-pm2); in a worker
thread the listener is not started ([§13](#worker-threads)).

---

## 11. OTLP endpoint, protocol, TLS and exporter options

All of these follow the shared contract; the variable table is in the
[README](README.md#otlp-export). In code:

```js
setup({
  otelEndpoint: 'https://otel-collector.example.com:4317', // all signals
  // Per-signal, wins for traces. gRPC uses only scheme://host:port (a path
  // is ignored); http/protobuf would use the whole URL as written.
  tracesEndpoint: 'https://traces.example.com:4317',
  otlpProtocol: 'grpc',            // or 'http/protobuf'
  exportTimeoutMs: 5000,           // integer in [1, 86 400 000]
  compression: 'gzip',             // or 'none'
});
```

- `http/protobuf`: `/v1/<signal>` is appended to the generic endpoint (a path
  prefix is kept, as written); per-signal endpoints are used as-is, path
  included.
- `grpc`: only `scheme://host:port` of any endpoint is used; a path is
  ignored without a warning (as the Go exporters do).
- Headers come from `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` only (no code
  option); put credentials there, never in the URL.
- gRPC over TLS needs a DNS name on Node.js 26 (an IP literal fails the
  handshake; warned on every version).
- An endpoint answering `Retry-After: 0` makes the SDK retry print Node's
  `TimeoutNegativeWarning` (see [Troubleshooting](#troubleshooting)).

---

## 12. `SetupOptions` and `Resolved` reference

Code values win over the environment, whatever the value. An invalid value
throws a `TypeError` naming the option, before anything is installed.

| Option | Type | Environment equivalent (— = code only) |
|---|---|---|
| `serviceName` | string | `OTEL_SERVICE_NAME` |
| `environment` | string | `deployment.environment.name` in `OTEL_RESOURCE_ATTRIBUTES` |
| `resourceAttributes` | `Record<string, string>` | `OTEL_RESOURCE_ATTRIBUTES` (each key wins) |
| `otelEndpoint`, `tracesEndpoint`, `metricsEndpoint`, `logsEndpoint` | string | `OTEL_EXPORTER_OTLP[_<SIGNAL>]_ENDPOINT` |
| `otlpProtocol` | `'grpc'` \| `'http/protobuf'` | `OTEL_EXPORTER_OTLP_PROTOCOL` |
| `insecure` | boolean | `OTEL_EXPORTER_OTLP_INSECURE` |
| `exportTimeoutMs` | integer ms | `OTEL_EXPORTER_OTLP_TIMEOUT` |
| `compression` | `'gzip'` \| `'none'` | `OTEL_EXPORTER_OTLP_COMPRESSION` |
| `metricExporters` | `string[]` (`[]` or `['none']` = off) | `OTEL_METRICS_EXPORTER` |
| `exportIntervalMs` | integer ms in [1, 2^31-1] | `OTEL_METRIC_EXPORT_INTERVAL` |
| `prometheusMetricsPort` | integer in [0, 65535] (`0` = no listener) | `OTEL_HELPER_METRICS_PORT` |
| `disabledMetrics` | `string[]` of globs | `OTEL_HELPER_DISABLED_METRICS` |
| `disabledSignals` | `('traces' \| 'metrics' \| 'logs')[]` | `OTEL_HELPER_DISABLED_SIGNALS` (deprecated; prefer `OTEL_<SIGNAL>_EXPORTER=none`) |
| `sampleRatio` | number in [0, 1] | `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `_ARG` |
| `healthPaths` | `string[]` | — |
| `debug` | boolean | `OTEL_HELPER_DEBUG_LEVEL` |
| `minimumLogLevel` | `'debug'` \| `'info'` \| `'warn'` \| `'error'` | — |
| `installDiagLogger` | boolean (default `true`) | — (`OTEL_LOG_LEVEL` sets its level) |
| `instrumentations` | `Instrumentation[]` | — |
| `replaceGlobals` | boolean (default `false`) | — (code only; [§13](#foreign-globals-and-replaceglobals)) |
| `spanProcessors` | `SpanProcessor[]` | — (tests and advanced use: added after the helper's own) |

`setup()` returns a frozen `Resolved` with the effective values, whatever set
them: `serviceName`, `environment` (`undefined` when absent),
`serviceInstanceId`, `debug`, `logLevel`, `otlp` (endpoint per signal),
`sdkDisabled`, `tracesExporters`, `metricExporters`, `logsExporters`,
`metricExportIntervalMs`, `prometheusMetricsPort`, `disabledMetrics`,
`sampler`, `propagators`, `healthPaths` and `inactiveSignals`. Headers are
never part of it (secrets).

---

## 13. Lifecycle: setup, shutdown, SIGTERM, cluster, worker threads

### Setup

- **Synchronous**, so it works from a `--require` preload.
- **Idempotent.** While a setup is active, later calls return its result and
  ignore their options (warned when they pass any). After `/register` ran, an app's own
  `setup({...})` is ignored the same way: preload your own file instead.
- **All-or-nothing.** A failure after the options were resolved releases
  everything that call built, leaves the globals as they were and is
  **thrown**; catch it to run without telemetry, or call `setup()` again.
  `/register` reports the failure instead of throwing.

```js
try {
  setup();
} catch (err) {
  console.error('telemetry disabled:', err.message); // the app keeps running
}
```

### Shutdown and SIGTERM

`shutdown(timeoutMs = 30000)` flushes the trace, metric and log providers
concurrently under one deadline, disables the instrumentations and stops the
`/metrics` listener. The first call's promise is cached; later calls return
it. It rejects with an `AggregateError` naming the providers that failed, or
an `Error` when the deadline passed.

**The helper installs no signal handler.** Node's default `SIGTERM` action
kills the process without flushing; handle it yourself:

```js
process.once('SIGTERM', () => { shutdown().finally(() => process.exit(0)); });
```

- **Keep `process.exit()` in `.finally`.** The deadline bounds the promise,
  not the process: a provider still exporting, or any other open handle
  (a server, a timer), can keep the process alive after it settles.
- The deadline timer keeps the process alive until the promise settles, so a
  provider that hangs keeps a process **up to `timeoutMs`** (default 30 s)
  after `shutdown()` — pass a deadline inside your grace period
  (`shutdown(5000)` under `docker stop`'s 10 s).
- Stop accepting work first (close the server, stop the worker loop), then
  shut down; the examples drain for at most 4 s, then call `shutdown(5000)`.
- `process.once` on purpose: a second signal gets Node's default action and
  exits at once, without the flush.
- A process that ends on its own is flushed by the helper's `beforeExit`
  listener ([§4](#short-lived-jobs-and-scripts)); `process.exit()` and
  signals skip it, so `await shutdown()` before `process.exit()` and keep the
  SIGTERM handler.

### Setup after shutdown

`setup()` after `shutdown()` rebuilds all three signals and points the
helper's forwarding providers at the new ones: tracers, meters, instruments
and loggers obtained earlier keep working (Go keeps earlier tracers on the
old provider; Python cannot re-enable traces and metrics).

- **Await `shutdown()` before calling `setup()` again.** A re-setup in the
  same tick can find the `/metrics` port still bound (`EADDRINUSE` reported on
  stderr), and it moves the observable and batch callbacks to the new
  provider, so the old provider's final export loses them.
- After `shutdown()` the helper's forwarders stay the API globals (pointing
  at nothing). To register **your own** TracerProvider, MeterProvider,
  LoggerProvider or propagator afterwards, call `trace.disable()`,
  `metrics.disable()`, `logs.disable()` or `propagation.disable()` first: the
  OpenTelemetry API refuses a second registration (Go allows it).

### Foreign globals and `replaceGlobals`

When another SDK or library registered a global TracerProvider,
MeterProvider, LoggerProvider or propagator before `setup()`, the helper
**keeps it** and warns:

```text
otelhelper: a global TracerProvider is already registered; the helper does not replace it and its traces are inactive signal=traces
```

- `Resolved.inactiveSignals` lists `traces` / `metrics` whose global provider
  is not the helper's (Node.js only). It covers global ownership only: a
  signal whose exporter failed to build is reported on stderr, not listed. A
  foreign LoggerProvider lists nothing: `createLogger()` still exports
  through the helper.
- The instrumentations bind to the foreign provider, so the helper's health
  drop and gRPC status rule do not apply to their spans.
- `/metrics` answers 503 while the MeterProvider is foreign.
- `setup({ replaceGlobals: true })` replaces the providers and the propagator
  (one INFO line each) — **never the context manager** (your asynchronous
  context depends on it; reported once). Tracers your app took from the
  replaced provider before `setup()` stay on it.

### Observable callbacks across re-setups

Callbacks registered on a meter from the global provider (`getMeter()`) move
to every new provider until you remove them. In tests,
`@staffops/otel-helper/testing` `reset()` discards them
([§15](#15-testing-your-app)).

### cluster and pm2

Each worker is a separate process with its own SDK; the primary does no app
work.

- **Every worker calls `setup()` itself**: at the top of the worker branch,
  or with `--import`/`--require @staffops/otel-helper/register` in the
  worker's `execArgv` (pm2: `node_args`). Never rely on the primary's.
- **Do not set up in the primary.** Any preload on the primary's command line
  (`node --import …/register primary.js`, or `NODE_OPTIONS=--require …/register`)
  sets up in the primary too: it then owns the metrics port and every worker
  reports `EADDRINUSE`. Start the primary without a preload and give it to
  the workers only:
  `cluster.setupPrimary({ execArgv: ['--import', '@staffops/otel-helper/register'] })`
  (checked: the worker is set up, the primary is not).
- **Leave `service.instance.id` unset**: each worker generates its own. An
  explicit one is warned per worker, unless you give each worker a different
  value. A restarted worker is a new process with a **new
  `service.instance.id`**, so new series appear after every restart.
- **Metrics, option 1 — a per-worker port from a stable slot** owned by the
  primary, re-forked on its slot only after an unexpected exit:

```js
const cluster = require('node:cluster');
const BASE = 9464;
let shuttingDown = false;

function fork(slot) {
  const w = cluster.fork({ OTEL_HELPER_METRICS_PORT: String(BASE + slot) });
  w.on('exit', () => { if (!w.exitedAfterDisconnect && !shuttingDown) fork(slot); });
}

if (cluster.isPrimary) {
  for (let slot = 0; slot < 4; slot++) fork(slot);
  process.once('SIGTERM', () => {
    shuttingDown = true;
    for (const w of Object.values(cluster.workers)) w.disconnect();
  });
} else {
  const { setup, shutdown } = require('@staffops/otel-helper');
  setup();
  require('./server');
  // disconnect() closes the worker's servers and IPC channel but sends no
  // signal: flush then. pm2 and process-group signals (Ctrl-C) reach the
  // worker directly: flush on those too.
  const flush = () => { shutdown().finally(() => process.exit(0)); };
  process.once('disconnect', flush);
  process.once('SIGTERM', flush);
  process.once('SIGINT', flush);
}
```

  A worker with nothing else open also ends on its own after `disconnect()`
  and is flushed by the helper's `beforeExit` listener; the handlers cover a
  worker that a timer, a pool or a socket keeps alive.

  Scrape one target per slot. Under pm2 use
  `BASE + Number(process.env.NODE_APP_INSTANCE)`. **Never `BASE +
  cluster.worker.id`**: worker ids are never reused, so a restarted worker
  moves to a port nobody scrapes and its metrics are lost silently. Each
  worker still logs `cluster worker serving /metrics on port <n>: if several
  workers use this port, …` once — expected on a per-worker port.
- **Metrics, option 2 — OTLP only**: set a metrics endpoint and leave
  `prometheus` out of `OTEL_METRICS_EXPORTER`. No listener starts; the
  Collector receives every worker's series, told apart by
  `service.instance.id`.
- **Never one shared 9464**: each scrape reaches one random worker.

pm2's cluster mode is built on `node:cluster`; it is not tested in this
repository. pm2 signals the workers itself (`SIGINT`, then `SIGKILL` after
`kill_timeout`, 1600 ms by default): raise `kill_timeout` above your
`shutdown()` deadline, or the flush is killed.

### Worker threads

- A worker thread is **not flushed when it ends** (the helper's `beforeExit`
  flush is the main thread's): a worker that called `setup()` must
  `await shutdown()` before its last task returns.
- A preload runs again in every **file-based** worker thread
  (`new Worker('./job.js')`), with `--import` and `--require` alike; in an
  `eval` worker (`new Worker(code, { eval: true })`) only `--require` runs
  (checked on Node 22.15.0, 24 and 26). In a worker,
  `@staffops/otel-helper/register` installs the hooks only, silently, and
  does not call `setup()`; a custom preload must check
  `require('node:worker_threads').isMainThread` before calling `setup()`
  ([§1](#1-getting-started)).
- Each thread has its own OpenTelemetry API globals: without its own
  `setup()`, what a worker thread records goes nowhere. A worker that wants
  telemetry calls `setup()` itself. It gets its own providers and **no `/metrics` listener**
  (one line on stderr; the main thread owns the port): export its metrics
  over OTLP or mount `metricsHandler()` on a server of that thread.
- `setup()` briefly hides the helper-owned OTLP variables from `process.env`
  while it builds the exporters; a worker started with `env: SHARE_ENV`
  during that window would see them missing. Start workers after `setup()`.

---

## 14. Validation and diagnostics

| Input | On error |
|---|---|
| A `setup()` option (wrong type, `sampleRatio` outside [0, 1], port outside [0, 65535], unknown exporter or signal, `insecure` contradicting a code URL, `http/json`) | `TypeError` naming the option; nothing is installed |
| An environment variable | one line on stderr naming the variable, fallback to the next level |
| A deprecated variable | one line per process naming the replacement |
| An OpenTelemetry SDK message | stderr through the helper's `DiagLogger`, level `OTEL_LOG_LEVEL` (default WARN) |

Lines start with `otelhelper:` and are written to `process.stderr` directly
(never `console.*`), so an app that patches `console` does not change them.
They never print header values, client keys, certificate paths or URL
credentials. Call `setup()` where a startup failure is visible, not lazily
inside a request. `installDiagLogger: false` leaves `diag` to your own
logger.

---

## 15. Testing your app

`@staffops/otel-helper/testing` (tests only — never in production code):

```js
const { test, beforeEach } = require('node:test');
const { setup } = require('@staffops/otel-helper');
const { reset, setDiagnosticSink, getResource } = require('@staffops/otel-helper/testing');

beforeEach(() => reset()); // forget the active setup, the globals it owns, "warned once" state and the sink

test('warns without a service name', () => {
  const lines = [];
  setDiagnosticSink((message) => lines.push(message));
  setup({ metricExporters: [] });
  // assert on lines, getResource().attributes ...
});
```

`reset()` unregisters only the globals the helper still owns and discards the
forwarding providers, so tracers, meters and callbacks of one test never
reach the next. To assert on spans, pass an in-memory exporter (SDK 3.0
processors take an options object). `@opentelemetry/sdk-trace` comes with the
helper, but require it from your tests only after adding it to your own
`devDependencies` (at the helper's `~3.0.0`), so the copy you load is one you
declared:

```js
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');

const exporter = new InMemorySpanExporter();
setup({ serviceName: 'test', metricExporters: [], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
// ... exporter.getFinishedSpans()
```

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `setup() runs in an ES module process without the module hooks` | Start with `--import @staffops/otel-helper/register`, or import `@staffops/otel-helper/hooks` first in your preload ([§1](#1-getting-started)) |
| `<module> was loaded before setup()` | `express` or `@grpc/grpc-js` was required first. Move `setup()` to a preload or the top of the entry ([§2](#routes-modules-after-setup)) |
| `http.route` is `/api` instead of `/api/orders/:id` | The router was built before `setup()` |
| No spans from an ioredis client in an ES module app | ioredis needs the loader hooks ([§9](#9-opt-in-instrumentation-packages)) |
| A gRPC call has no SERVER span | The handler threw synchronously ([§3](#3-grpc)) |
| A worker cycle's root span never arrives | Its promise never settled ([§4](#4-workers-and-background-jobs)) |
| Two `traceparent` headers; the callee starts a new trace | `propagation.inject()` into an instrumented `fetch` ([§2](#outbound-http-and-propagation)) |
| `the /metrics listener could not bind port 9464` | Another process (or the `cluster` primary) owns it; set a per-process port or mount `metricsHandler()` with port `0` |
| `EADDRINUSE` right after a re-setup | `setup()` ran before the previous `shutdown()` settled: await it |
| `/metrics` answers 503 | No setup, a failed one, after `shutdown()`, or a foreign MeterProvider (`Resolved.inactiveSignals`) |
| `a global TracerProvider is already registered` | Another SDK registered first; remove it or pass `replaceGlobals: true` |
| gRPC export fails on Node 26 with an IP endpoint | Node refuses an IP as the TLS server name; use a DNS name |
| Nothing exported from a short-lived job | It ended with `process.exit()` (or was killed) before a flush: `await shutdown()` first. A job that just returns is flushed by the helper's `beforeExit` listener ([§4](#short-lived-jobs-and-scripts)) |
| `shutdown()` takes the whole timeout; nothing arrives | The OTLP endpoint is unreachable: at the default level the SDK's error shows on stderr as `otelhelper: opentelemetry diag level=error … UNAVAILABLE …` (gRPC). A refused connection fails fast; a blackholed one waits for each export's timeout (`OTEL_EXPORTER_OTLP_TIMEOUT`, 10 s). Check the endpoint, port and protocol (4317 gRPC, 4318 http/protobuf) and the TLS scheme; `OTEL_LOG_LEVEL=DEBUG` adds the SDK's detail |
| Process lingers after finishing | The helper's `beforeExit` flush is exporting to a slow or unreachable Collector (up to the export timeout, at most the 30 s shutdown deadline). Call `await shutdown(5000)` yourself at the end ([§4](#short-lived-jobs-and-scripts)) |
| `TimeoutNegativeWarning` on stderr | The OTLP endpoint answered `Retry-After: 0`; the SDK's retry passes a negative delay to `setTimeout` (harmless, Node clamps it to 1 ms). Fix the endpoint's rate limiting |
| The process stays up after `shutdown()` | Another handle is open, or a provider is still exporting: exit in `.finally` ([§13](#shutdown-and-sigterm)) |
| Log lines appear twice | pino destination in a `multistream` next to `process.stdout` ([§5](#pino)) |
| Counters export `Double` values | Pass `valueType: ValueType.INT` ([§6](#metrics)) |

---

## FAQ

**Why no runtime metrics (event loop, heap, GC)?** Runtime metrics are
language-specific and out of the shared contract; the helper emits none
(Go `go.*` and Python `system.*`/`process.*` come from their own
instrumentations); runtime metrics for Node.js are out of scope for the helper.

**Can I use `NodeSDK` or `@opentelemetry/auto-instrumentations-node` too?**
No: both register global providers. When they register first, the helper
keeps theirs and its own signals are inactive (`Resolved.inactiveSignals`)
unless `replaceGlobals` is set; registering after the helper is refused by
the OpenTelemetry API. Use one or the other.

**Why does `OTEL_SEMCONV_STABILITY_OPT_IN=http/dup` warn?** The Node.js HTTP
instrumentations no longer have the old conventions: they always emit the
stable ones.

**Are exemplars supported?** No: `@opentelemetry/sdk-metrics` 3.0 exports no
exemplar filter, so `OTEL_METRICS_EXEMPLAR_FILTER` is not honored.

**Does the helper read the declarative configuration file?** No:
`OTEL_CONFIG_FILE` is warned and ignored, as in Go and Python.
