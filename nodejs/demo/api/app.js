// demo-node-api: HTTP server (express, POST /orders), Redis cache client and
// queue producer (node-redis through @staffops/otel-helper-redis), gRPC
// client of demo-node-backend. specs/demo-environment DE3c; span
// demo.order.accept, PRODUCER span (DE-D6), ADR-014 failure for the
// unhandled scenario through the helper's withSpan (DE-D8, A45). Settings from the environment only:
//   DEMO_HTTP_PORT (8080), DEMO_BACKEND_ADDR (demo-node-backend:50051),
//   DEMO_REDIS_ADDR (demo-redis:6379), DEMO_REDIS_PASSWORD_FILE (required).
//
// Loaded by main.js after setup(): every
// instrumented library (express, @grpc/grpc-js and redis) is required here, after setup().
'use strict';

const { getTracer, shutdown, withSpan } = require('@staffops/otel-helper');

// withSpan is the A45 span function (contract design D15); the published
// 0.3.0-rc.1 pins in package.json predate it. Without it every failure would
// go unrecorded, so stop here instead.
if (typeof withSpan !== 'function') {
  throw new Error('@staffops/otel-helper has no withSpan (A45): build the demo image from the workspace '
    + '(the Dockerfile packs nodejs/packages over the published pins)');
}

const http = require('node:http');
const { SpanStatusCode, context: contextApi } = require('@opentelemetry/api');
const express = require('express');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');
const { createClient } = require('redis');

const { loadOrderService } = require('../lib/assets');
const { healthClient, isServing } = require('../lib/health');
const { LANG, envOr, parseHostPort, portEnv, readSecretFile, runId } = require('../lib/config');
const { DemoUnhandledError } = require('../lib/errors');
const { DEMO_SCOPE, errorTypeOf, markPropagated } = require('../lib/failure');
const { httpStatusForGrpc } = require('../lib/grpc-status');
const { createDemoLogger } = require('../lib/logging');
const { injectCarrier, producerSpan, quoteKey, streamKey } = require('../lib/messaging');
const { parseOrderBody } = require('../lib/orders');

const env = process.env;
const RUN = runId(env);
const HTTP_PORT = portEnv(env, 'DEMO_HTTP_PORT', 8080);
const BACKEND_ADDR = envOr(env, 'DEMO_BACKEND_ADDR', `demo-${LANG}-backend:50051`);
const REDIS = parseHostPort(envOr(env, 'DEMO_REDIS_ADDR', 'demo-redis:6379'), 6379, 'DEMO_REDIS_ADDR');
const STREAM = streamKey(LANG);
const BACKEND_DEADLINE_MS = 5_000;
const QUOTE_TTL_SECONDS = 30;
const STREAM_MAXLEN = 1000;

const tracer = getTracer(DEMO_SCOPE);
const log = createDemoLogger('demo-node-api');

// Password as an option, never in a URL (no userinfo anywhere, DR12).
const redis = createClient({
  socket: { host: REDIS.host, port: REDIS.port, connectTimeout: 5_000 },
  password: readSecretFile(env, 'DEMO_REDIS_PASSWORD_FILE'),
});
redis.on('error', (err) => log.warn({ 'error.type': errorTypeOf(err) }, 'redis connection error'));

const OrderService = loadOrderService(grpc, protoLoader);
const backend = new OrderService(BACKEND_ADDR, grpc.credentials.createInsecure());
// grpc.health.v1 calls produce no span (the helper filters them, HOW-TO §3).
const backendHealth = healthClient(grpc, protoLoader, BACKEND_ADDR);
const HEALTH_TIMEOUT_MS = 1_000;

/** One PlaceOrder call with a deadline; resolves `{reply}` or `{code}` (never rejects). */
function placeOrder(order) {
  return new Promise((resolve) => {
    backend.PlaceOrder(order, { deadline: Date.now() + BACKEND_DEADLINE_MS }, (err, reply) => {
      resolve(err ? { code: err.code } : { reply });
    });
  });
}

/**
 * The customer's cached quote; a miss computes and caches it for 30 s. A
 * cache failure is not fatal: one WARN, the order goes on (DE-D7 r3.5).
 */
async function refreshQuote(customer) {
  try {
    const key = quoteKey(LANG, customer);
    if ((await redis.get(key)) !== null) return;
    await redis.set(key, JSON.stringify({ customer, tier: 'standard' }), { expiration: { type: 'EX', value: QUOTE_TTL_SECONDS } });
  } catch (err) {
    log.warn({ 'error.type': errorTypeOf(err) }, 'quote cache unavailable');
  }
}

/**
 * PRODUCER span around XADD, the current context injected into the message
 * fields; the entry carries order_id and scenario only besides the
 * propagation fields (DE-D6, DE-D7 r3.5). A failed publish is not fatal: the
 * span is ERROR without an event of its own, one WARN after it ends, and the
 * answer stays 201.
 */
async function publish(order) {
  const { name, options } = producerSpan(STREAM, REDIS);
  const failure = await tracer.startActiveSpan(name, options, async (span) => {
    try {
      const fields = { ...injectCarrier(contextApi.active()), order_id: order.order_id, scenario: order.scenario };
      const id = await redis.xAdd(STREAM, '*', fields, { TRIM: { strategy: 'MAXLEN', strategyModifier: '~', threshold: STREAM_MAXLEN } });
      span.setAttribute('messaging.message.id', String(id));
      return undefined;
    } catch (err) {
      span.setStatus({ code: SpanStatusCode.ERROR });
      return err;
    } finally {
      span.end();
    }
  });
  if (failure !== undefined) log.warn({ 'error.type': errorTypeOf(failure) }, 'order event not published');
}

/**
 * The work of POST /orders in demo.order.accept, run by the helper's withSpan
 * (contract design D15). The unhandled scenario throws here: withSpan marks
 * the span (status ERROR without description, error.type DemoUnhandledError,
 * no exception event), writes the one ERROR record with the message and the
 * stack in its context, ends it, and only then rethrows for express to answer
 * 500 (DE-D7 r3.4: end the span before re-raising).
 */
function accept(order) {
  return withSpan('demo.order.accept', async (span) => {
    if (order.scenario === 'unhandled') throw new DemoUnhandledError(`boom unh-${RUN}`);
    await refreshQuote(order.customer);
    const placed = await placeOrder(order);
    if (placed.code !== undefined) {
      // The backend owns the failure (its span and record name it): ERROR
      // without description, error.type = the answered HTTP status (as the
      // worker's demo.drive), no event, no record here (DE-D8 note of
      // 2026-11-17); 422 / 409 for the business codes, 502 for any other (r3.5).
      const status = httpStatusForGrpc(placed.code);
      markPropagated(span, status);
      return { status, body: { detail: http.STATUS_CODES[status] } };
    }
    await publish(order);
    return { status: 201, body: { order_id: placed.reply.order_id, status: placed.reply.status } };
  }, { tracer, attributes: { 'demo.scenario': order.scenario, 'order.id': order.order_id } });
}

const app = express();
app.use(express.json({ limit: '16kb' }));

// 200 only when the listener is up, Redis is connected and the backend's
// grpc.health.v1 answers SERVING; else 503 (DE-D7 r3.5, DE-D11 r3.6).
let listening = false;
app.get('/health', async (_req, res) => {
  const ready = listening && redis.isReady && (await isServing(backendHealth, 'demo.v1.OrderService', HEALTH_TIMEOUT_MS));
  res.status(ready ? 200 : 503).json({ status: ready ? 'ok' : 'starting' });
});

app.post('/orders', async (req, res) => {
  const parsed = parseOrderBody(req.body);
  if (parsed.error) {
    res.status(400).json({ detail: parsed.error });
    return;
  }
  const out = await accept(parsed.order);
  res.status(out.status).json(out.body);
});

app.use((_req, res) => {
  res.status(404).json({ detail: 'Not Found' });
});

// Final error handler: maps an escaped error to 500 (a malformed body to its
// 4xx) with a fixed text. It logs nothing: the owning span already wrote the
// ERROR record (DE-D8), and express's default handler would print the stack
// to stderr outside any span.
app.use((err, _req, res, next) => {
  const status = Number.isInteger(err?.status) && err.status >= 400 && err.status < 500 ? err.status : 500;
  if (res.headersSent) return next(err);
  res.status(status).json({ detail: http.STATUS_CODES[status] ?? 'Error' });
});

let server;
let stopping = false;
function stop(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  const drained = new Promise((resolve) => {
    if (server === undefined) return resolve();
    server.close(() => resolve());
    setTimeout(resolve, 4_000).unref();
  });
  drained
    .then(async () => {
      backend.close();
      backendHealth.close();
      await redis.close().catch(() => {});
      await new Promise((resolve) => setTimeout(resolve, 100));
      return shutdown(5_000);
    })
    .catch((err) => console.error('telemetry shutdown incomplete:', errorTypeOf(err)))
    .finally(() => process.exit(exitCode));
}
process.once('SIGTERM', () => stop());
process.once('SIGINT', () => stop());

async function main() {
  await redis.connect();
  server = app.listen(HTTP_PORT, '0.0.0.0', (err) => {
    if (err) return;
    listening = true;
    log.info({ 'server.port': HTTP_PORT }, 'demo api listening');
  });
  server.on('error', (err) => {
    log.error({ err }, 'HTTP listen failed');
    stop(1);
  });
}

main().catch((err) => {
  log.error({ err }, 'demo api failed to start');
  stop(1);
});
