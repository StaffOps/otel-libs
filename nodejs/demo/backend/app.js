// demo-node-backend: gRPC server (demo.v1.OrderService) with a PostgreSQL
// client (pg through @staffops/otel-helper-sql). specs/demo-environment
// DE3c; span demo.order.process, custom metrics (DE-D9), ADR-014 failures
// (DE-D8). Settings from the environment only (DE-D2, DE-D13):
//   DEMO_GRPC_PORT (50051), DEMO_PG_HOST (demo-postgres, host[:port]),
//   DEMO_PG_DB (demo_node), DEMO_PG_USER (demo_node), DEMO_PG_PASSWORD_FILE
//   (required), DEMO_SCHEMA_FILE (default
//   /etc/otel-libs-demo/schema.sql, baked into the image). Serves
//   grpc.health.v1.Health next to demo.v1.OrderService.
//
// Loaded by main.js once the helper is set up (lib/bootstrap.js): every
// instrumented library (pg and @grpc/grpc-js) is required here, after setup().
'use strict';

const { getMeter, getTracer, shutdown } = require('@staffops/otel-helper');

const { setTimeout: sleep } = require('node:timers/promises');
const { ValueType } = require('@opentelemetry/api');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');
const pg = require('pg');

const { loadOrderService, readSchema } = require('../lib/assets');
const { healthHandlers, healthServiceDefinition } = require('../lib/health');
const { LANG, envOr, parseHostPort, portEnv, readSecretFile, runId } = require('../lib/config');
const { DEMO_SCOPE, errorTypeOf, ownFailure } = require('../lib/failure');
const { CODE, grpcErrorFor } = require('../lib/grpc-status');
const { createDemoLogger } = require('../lib/logging');
const { INSERT_SQL, SELECT_SQL, UPSERT_SQL, isBackendScenario, processOrder, toOrderMessage } = require('../lib/orders');

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

/** PlaceOrder in the owning span demo.order.process; the reply goes out after the span ended. */
async function placeOrder(request) {
  // An unknown scenario is a caller error, not a processing failure (DE-D7 r3.5).
  if (!isBackendScenario(request.scenario)) return { error: { code: CODE.INVALID_ARGUMENT, details: 'invalid scenario' } };
  return tracer.startActiveSpan('demo.order.process', {
    attributes: { 'demo.scenario': request.scenario, 'order.id': request.order_id },
  }, async (span) => {
    const started = performance.now();
    let attrs = { 'demo.scenario': request.scenario, 'demo.outcome': 'ok' };
    try {
      const row = await processOrder(request, RUN, db);
      log.info({ 'order.id': row.id, 'demo.scenario': request.scenario }, 'order processed');
      return { reply: { order_id: row.id, status: 'processed' } };
    } catch (err) {
      const type = ownFailure(span, log, err, { 'demo.scenario': request.scenario });
      attrs = { ...attrs, 'demo.outcome': 'error', 'error.type': type };
      return { error: grpcErrorFor(err) };
    } finally {
      // Recorded while demo.order.process is the active span (trace_based exemplars, DE-D9).
      duration.record((performance.now() - started) / 1000, attrs);
      processed.add(1, attrs);
      span.end();
    }
  });
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
