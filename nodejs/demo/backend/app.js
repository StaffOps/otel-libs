// demo-node-backend: gRPC server (demo.v1.OrderService) with a PostgreSQL
// client (pg through @staffops/otel-helper-sql). specs/demo-environment
// DE3c; span demo.order.process, custom metrics (DE-D9), ADR-014 failures
// through the helper's withSpan (DE-D8, A45). Settings from the environment only (DE-D2, DE-D13):
//   DEMO_GRPC_PORT (50051), DEMO_PG_HOST (demo-postgres, host[:port]),
//   DEMO_PG_DB (demo_node), DEMO_PG_USER (demo_node), DEMO_PG_PASSWORD_FILE
//   (required), DEMO_SCHEMA_FILE (default
//   /etc/otel-libs-demo/schema.sql, baked into the image). Serves
//   grpc.health.v1.Health next to demo.v1.OrderService.
//
// Loaded by main.js after setup(): every
// instrumented library (pg and @grpc/grpc-js) is required here, after setup().
'use strict';

const { getMeter, getTracer, shutdown, withSpan } = require('@staffops/otel-helper');

// withSpan is the A45 span function (contract design D15); the published
// 0.3.0-rc.1 pins in package.json predate it. Without it every failure would
// go unrecorded, so stop here instead.
if (typeof withSpan !== 'function') {
  throw new Error('@staffops/otel-helper has no withSpan (A45): build the demo image from the workspace '
    + '(the Dockerfile packs nodejs/packages over the published pins)');
}

const { setTimeout: sleep } = require('node:timers/promises');
const { ValueType } = require('@opentelemetry/api');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');
const pg = require('pg');

const { loadOrderService, readSchema } = require('../lib/assets');
const { healthHandlers, healthServiceDefinition } = require('../lib/health');
const { LANG, envOr, parseHostPort, portEnv, readSecretFile, runId } = require('../lib/config');
const { DEMO_SCOPE, errorTypeOf } = require('../lib/failure');
const { CODE } = require('../lib/grpc-status');
const { createDemoLogger } = require('../lib/logging');
const { INSERT_SQL, SELECT_SQL, UPSERT_SQL, toOrderMessage } = require('../lib/orders');
const { placeOrderInSpan } = require('../lib/place-order');

const env = process.env;
const RUN = runId(env);
const GRPC_PORT = portEnv(env, 'DEMO_GRPC_PORT', 50051);
const PG = parseHostPort(envOr(env, 'DEMO_PG_HOST', 'demo-postgres'), 5432, 'DEMO_PG_HOST');

const tracer = getTracer(DEMO_SCOPE);
const meter = getMeter(DEMO_SCOPE);
const log = createDemoLogger('demo-node-backend');

// DE-D9: integer counter and a seconds histogram with the shared boundaries.
const processed = meter.createCounter('demo.orders.processed', {
  valueType: ValueType.INT,
  unit: '{order}',
  description: 'Orders processed by the demo backend',
});
const duration = meter.createHistogram('demo.order.duration', {
  unit: 's',
  description: 'Time spent processing one order in the demo backend',
  advice: { explicitBucketBoundaries: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5] },
});

// The password stays in memory: the pool gets it as an option, never in a
// connection string, and nothing logs the pool's configuration.
const pool = new pg.Pool({
  host: PG.host,
  port: PG.port,
  database: envOr(env, 'DEMO_PG_DB', `demo_${LANG}`),
  user: envOr(env, 'DEMO_PG_USER', `demo_${LANG}`),
  password: readSecretFile(env, 'DEMO_PG_PASSWORD_FILE'),
  max: 4,
  connectionTimeoutMillis: 5_000,
  idleTimeoutMillis: 30_000,
});
pool.on('error', (err) => log.warn({ 'error.type': errorTypeOf(err) }, 'idle database connection failed'));

const db = {
  upsert: (id, customer, amount, scenario) => pool.query(UPSERT_SQL, [id, customer, amount, scenario]),
  insert: (id, customer, amount, scenario) => pool.query(INSERT_SQL, [id, customer, amount, scenario]),
  select: async (id) => (await pool.query(SELECT_SQL, [id])).rows[0],
};

/** PlaceOrder in the owning span demo.order.process (lib/place-order.js, DE-D8, DE-D9). */
function placeOrder(request) {
  return placeOrderInSpan({ withSpan, tracer, db, run: RUN, log, processed, duration }, request);
}

async function getOrder(request) {
  const row = await db.select(request.order_id);
  return row === undefined
    ? { error: { code: CODE.NOT_FOUND, details: 'order not found' } }
    : { reply: toOrderMessage(row) };
}

/** A unary handler from an async function: errors go through the callback, never thrown (HOW-TO §3). */
function unary(fn) {
  return (call, callback) => {
    fn(call.request)
      .then((out) => (out.error ? callback(out.error) : callback(null, out.reply)))
      .catch(() => callback({ code: CODE.INTERNAL, details: 'order failed' }));
  };
}

/** Applies the schema (DEMO_SCHEMA_FILE) as one command without bind parameters (simple query protocol, DE-D4). */
async function applySchema() {
  const schema = readSchema();
  for (let attempt = 1; ; attempt += 1) {
    try {
      await pool.query(schema);
      log.info({ attempt }, 'schema applied');
      return;
    } catch (err) {
      if (attempt >= 30) throw err;
      log.warn({ attempt, 'error.type': errorTypeOf(err) }, 'schema not applied yet, retrying');
      await sleep(2_000);
    }
  }
}

const OrderService = loadOrderService(grpc, protoLoader);
const server = new grpc.Server();
server.addService(OrderService.service, { PlaceOrder: unary(placeOrder), GetOrder: unary(getOrder) });
// Bound only after the schema is applied, so SERVING means ready.
server.addService(healthServiceDefinition(grpc, protoLoader), healthHandlers(
  (service) => (service === '' || service === 'demo.v1.OrderService' ? 'SERVING' : undefined),
  CODE.NOT_FOUND,
));

let stopping = false;
function stop(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  const drained = new Promise((resolve) => {
    server.tryShutdown(() => resolve());
    setTimeout(() => { server.forceShutdown(); resolve(); }, 4_000).unref();
  });
  drained
    .then(() => pool.end().catch(() => {}))
    .then(() => shutdown(5_000))
    .catch((err) => console.error('telemetry shutdown incomplete:', errorTypeOf(err)))
    .finally(() => process.exit(exitCode));
}
process.once('SIGTERM', () => stop());
process.once('SIGINT', () => stop());

async function main() {
  await applySchema();
  await new Promise((resolve, reject) => {
    server.bindAsync(`0.0.0.0:${GRPC_PORT}`, grpc.ServerCredentials.createInsecure(), (err) => (err ? reject(err) : resolve()));
  });
  log.info({ 'server.port': GRPC_PORT }, 'demo backend listening');
}

main().catch((err) => {
  log.error({ err }, 'demo backend failed to start');
  stop(1);
});
