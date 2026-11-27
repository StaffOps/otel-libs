// demo-node-worker: load driver (POST /orders to demo-node-api on the
// scenario cycle, root span demo.drive) and queue consumer (Redis Streams,
// CONSUMER span with a link to and parent = the producer). specs/demo-environment
// DE3c; DE-D5, DE-D6, DE-D7, DE-D8. Settings from the environment only:
//   DEMO_API_URL (base URL, http://demo-node-api:8080), DEMO_REDIS_ADDR
//   (demo-redis:6379),
//   DEMO_REDIS_PASSWORD_FILE, DEMO_INTERVAL_SECONDS (5), DEMO_POLL_SECONDS (2).
//
// Loaded by main.js after setup(): every
// instrumented library (redis; fetch's undici) is required here, after setup().
'use strict';

const { getTracer, shutdown, startRootSpan } = require('@staffops/otel-helper');

const os = require('node:os');
const { setTimeout: sleep } = require('node:timers/promises');
const { createClient } = require('redis');

const { LANG, envOr, parseHostPort, readSecretFile, secondsEnv } = require('../lib/config');
const { DEMO_SCOPE, errorTypeOf, markPropagated } = require('../lib/failure');
const { createDemoLogger } = require('../lib/logging');
const { CONSUMER_GROUP, consumerSpan, streamKey } = require('../lib/messaging');
const { waitUntil, withoutTracing } = require('../lib/quiet');
const { buildOrder } = require('../lib/scenarios');

const env = process.env;
// DEMO_API_URL is a base URL; /orders is appended to it (a base path is kept, DE-D7 r3.4).
const API_BASE = envOr(env, 'DEMO_API_URL', `http://demo-${LANG}-api:8080`).replace(/\/+$/, '');
const API_URL = `${API_BASE}/orders`;
const HEALTH_URL = `${API_BASE}/health`;
const REDIS = parseHostPort(envOr(env, 'DEMO_REDIS_ADDR', 'demo-redis:6379'), 6379, 'DEMO_REDIS_ADDR');
const INTERVAL_MS = secondsEnv(env, 'DEMO_INTERVAL_SECONDS', 5) * 1000;
const POLL_MS = secondsEnv(env, 'DEMO_POLL_SECONDS', 2) * 1000;
const STREAM = streamKey(LANG);
const CONSUMER = os.hostname();
const REQUEST_TIMEOUT_MS = 10_000;
const READ_COUNT = 10;

const tracer = getTracer(DEMO_SCOPE);
const log = createDemoLogger('demo-node-worker');

const redis = createClient({
  socket: { host: REDIS.host, port: REDIS.port, connectTimeout: 5_000 },
  password: readSecretFile(env, 'DEMO_REDIS_PASSWORD_FILE'),
});
redis.on('error', (err) => log.warn({ 'error.type': errorTypeOf(err) }, 'redis connection error'));

let stopping = false;

/** One request of the cycle in its own root span demo.drive (DE-D7, DE-D8 rule). */
async function drive(seq) {
  const order = buildOrder(seq);
  await startRootSpan('demo.drive', async (span) => {
    let status;
    try {
      const res = await fetch(API_URL, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(order),
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      status = res.status;
      await res.arrayBuffer(); // drain, so the connection is reused
    } catch (err) {
      // No answer at all: the exception type name of the root cause (DE-D7 r3.5).
      const type = errorTypeOf(rootCause(err));
      markPropagated(span, type);
      span.setAttribute('demo.outcome', 'error');
      log.warn({ 'error.type': type }, 'order request failed');
      return;
    }
    if (status >= 200 && status < 300) {
      span.setAttribute('demo.outcome', 'ok');
      return;
    }
    markPropagated(span, status);
    span.setAttribute('demo.outcome', 'error');
    log.warn({ 'http.response.status_code': status }, 'order request failed');
  }, { tracer, attributes: { 'demo.scenario': order.scenario } });
}

/** The innermost `cause` of an error chain (bounded, cycles stop it). */
function rootCause(err) {
  let current = err;
  for (let i = 0; i < 10 && current instanceof Error && current.cause !== undefined; i += 1) current = current.cause;
  return current;
}

let driveTimer;
/** One request per tick at a fixed rate (not sleep-after-work), seq from 1 (DE-D7 r3.5). */
function startDriving() {
  let seq = 0;
  const tick = () => {
    seq += 1;
    drive(seq).catch((err) => log.warn({ 'error.type': errorTypeOf(err) }, 'drive cycle failed'));
  };
  tick();
  driveTimer = setInterval(() => { if (!stopping) tick(); }, INTERVAL_MS);
}

/** Creates the consumer group (and the stream) unless it exists; no command fails on the normal path. */
async function ensureGroup() {
  if (await redis.exists(STREAM)) {
    const groups = await redis.xInfoGroups(STREAM);
    if (groups.some((g) => String(g.name) === CONSUMER_GROUP)) return;
  }
  try {
    await redis.xGroupCreate(STREAM, CONSUMER_GROUP, '$', { MKSTREAM: true });
  } catch (err) {
    // Another worker created it between the check and the create.
    if (!String(err?.message ?? '').startsWith('BUSYGROUP')) throw err;
  }
}

/** One message in its CONSUMER span: the record and the XACK are inside it (DE-D6, DR4). */
async function processMessage(message) {
  const fields = message.message ?? {};
  const { name, parent, options } = consumerSpan(STREAM, REDIS, String(message.id), fields);
  await tracer.startActiveSpan(name, options, parent, async (span) => {
    try {
      log.info({
        'order.id': String(fields.order_id ?? ''),
        'demo.scenario': String(fields.scenario ?? ''),
      }, 'order event processed');
      await redis.xAck(STREAM, CONSUMER_GROUP, message.id);
    } finally {
      span.end();
    }
  });
}

/**
 * XREADGROUP COUNT 10 without BLOCK every DEMO_POLL_SECONDS (DE-D5). The read
 * always runs with tracing suppressed, as in the other languages: it happens
 * before any producer context exists, so it cannot sit under a CONSUMER span,
 * and an idle poll would be a root span (DE-D11 r3.6). Each message's CONSUMER
 * span and its XACK are traced.
 */
async function consumeLoop() {
  while (!stopping) {
    const started = Date.now();
    try {
      const reply = await withoutTracing(() => redis.xReadGroup(CONSUMER_GROUP, CONSUMER, { key: STREAM, id: '>' }, { COUNT: READ_COUNT }));
      for (const stream of reply ?? []) {
        for (const message of stream.messages ?? []) await processMessage(message);
      }
    } catch (err) {
      log.warn({ 'error.type': errorTypeOf(err) }, 'queue poll failed');
      // NOGROUP after a Redis restart: create the group again.
      if (String(err?.message ?? '').startsWith('NOGROUP')) await ensureGroup().catch(() => {});
    }
    await sleep(Math.max(0, POLL_MS - (Date.now() - started)));
  }
}

function stop(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  clearInterval(driveTimer);
  // Let the current request and poll finish (both bounded by their timeouts).
  setTimeout(() => {
    redis.close().catch(() => {})
      .then(() => shutdown(5_000))
      .catch((err) => console.error('telemetry shutdown incomplete:', errorTypeOf(err)))
      .finally(() => process.exit(exitCode));
  }, 1_000);
}
process.once('SIGTERM', () => stop());
process.once('SIGINT', () => stop());

async function main() {
  await redis.connect();
  await ensureGroup();
  log.info({ 'messaging.destination.name': STREAM }, 'demo worker started');
  // The first demo.drive waits for the api's /health 200 (Redis and backend
  // ready), probed with tracing suppressed (DE-D11 r3.6: start-up race).
  const ready = await waitUntil(() => withoutTracing(async () => {
    const res = await fetch(HEALTH_URL, { signal: AbortSignal.timeout(2_000) });
    await res.arrayBuffer();
    return res.status === 200;
  }), { intervalMs: 1_000, shouldStop: () => stopping, onWait: () => log.info({}, 'waiting for the api to be ready') });
  if (!ready) return;
  log.info({}, 'api ready, driving orders');
  startDriving();
  await consumeLoop();
}

main().catch((err) => {
  log.error({ err }, 'demo worker failed');
  stop(1);
});
