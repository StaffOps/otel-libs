// Notification API: an express service that receives notification requests and
// forwards them to node-backend over gRPC. Node twin of python-api.
'use strict';

// The helper first: express and @grpc/grpc-js must be loaded after setup(),
// so their instrumentations patch them and the routes get a full http.route.
const { createLogger, getMeter, getTracer, setup, shutdown } = require('@staffops/otel-helper');

setup({ resourceAttributes: { 'app.component': 'notification-api' } });

const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { SpanStatusCode } = require('@opentelemetry/api');
const express = require('express');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');

const tracer = getTracer('node-api');
const meter = getMeter('node-api');

// Legacy instrument names, kept identical to python-api (see the README).
const notificationsSent = meter.createCounter('notifications.sent_total');
const notificationsFailed = meter.createCounter('notifications.failed_total');
const templateRenders = meter.createCounter('templates.rendered_total');
const activeRequests = meter.createUpDownCounter('notifications.active_requests');

const logger = createLogger({ name: 'node-api' });

const PORT = Number(process.env.PORT ?? 8000);
const BACKEND_URL = process.env.BACKEND_URL ?? 'node-backend-active:50051';
const BACKEND_DEADLINE_MS = 5_000;

/** `String(value)` that never throws (`Object.create(null)`, a throwing `toString`). */
function safeString(value) {
  try {
    return String(value);
  } catch {
    try {
      return Object.prototype.toString.call(value);
    } catch {
      return 'unknown error';
    }
  }
}

/** `{ name, message, stack }` of a thrown value, never throwing; `name` is undefined for a non-Error. */
function describeError(err) {
  let isError = false;
  try {
    isError = err instanceof Error;
  } catch {
    // a hostile Proxy: treated as a non-Error value
  }
  if (!isError) return { message: safeString(err) };
  const stack = typeof err.stack === 'string' ? err.stack : undefined;
  return { name: safeString(err.name), message: safeString(err.message), ...(stack === undefined ? {} : { stack }) };
}

/**
 * Exception event + ERROR on `span`. The event gets a plain `{ name, message,
 * stack }`, so `exception.type` is the error's name — the SDK would otherwise
 * prefer `err.code` (a gRPC status number such as 14, an errno string).
 */
function recordError(span, err) {
  const e = describeError(err);
  span.recordException(e.name === undefined ? e.message : e);
  span.setStatus({ code: SpanStatusCode.ERROR, message: e.name === undefined ? e.message : `${e.name}: ${e.message}` });
}

/** Log attributes of a thrown value: the type and the bare message (no type prefix). */
function errorAttributes(err) {
  const e = describeError(err);
  return { 'exception.type': e.name ?? typeof err, 'exception.message': e.message };
}

// --- gRPC client (proto loaded at runtime, no generated stubs) ---
// NOTIFICATION_PROTO, else ./protos (the image layout), else ../protos (a source checkout).
const PROTO_PATH = process.env.NOTIFICATION_PROTO
  ?? [path.join(__dirname, 'protos', 'notification.proto'), path.join(__dirname, '..', 'protos', 'notification.proto')]
    .find((p) => fs.existsSync(p))
  ?? path.join(__dirname, 'protos', 'notification.proto');
const { notification } = grpc.loadPackageDefinition(
  protoLoader.loadSync(PROTO_PATH, { keepCase: true, longs: String, enums: String, defaults: true }),
);
// One channel for the process (python-api opens one per request; grpc-js
// channels are meant to be shared).
const backend = new notification.NotificationService(BACKEND_URL, grpc.credentials.createInsecure());

/** One unary call with a deadline, so a stuck backend cannot hold a request (or shutdown) forever. */
function callBackend(method, request) {
  return new Promise((resolve, reject) => {
    backend[method](request, { deadline: Date.now() + BACKEND_DEADLINE_MS }, (err, reply) => (err ? reject(err) : resolve(reply)));
  });
}

// --- In-memory store ---
const templatesDb = new Map([
  ['default', { name: 'default', body: 'Hello {{name}}', subject: 'Notification' }],
  ['welcome', { name: 'welcome', body: 'Welcome {{name}} to {{product}}', subject: 'Welcome!' }],
  ['alert', { name: 'alert', body: 'Alert: {{message}}', subject: '⚠️ Alert' }],
]);
const notificationsDb = new Map();

const isPlainObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

/**
 * Validates the POST /notify body like python-api's pydantic model (422 on
 * error): an omitted field takes its default, an explicit null is invalid.
 */
function parseNotify(body) {
  if (!isPlainObject(body)) return { error: 'body: must be a JSON object' };
  if (typeof body.recipient !== 'string') return { error: 'recipient: field required (string)' };
  const req = {
    recipient: body.recipient,
    channel: body.channel === undefined ? 'email' : body.channel,
    template_id: body.template_id === undefined ? 'default' : body.template_id,
    variables: body.variables === undefined ? {} : body.variables,
  };
  if (typeof req.channel !== 'string') return { error: 'channel: must be a string' };
  if (typeof req.template_id !== 'string') return { error: 'template_id: must be a string' };
  if (!isPlainObject(req.variables) || !Object.values(req.variables).every((v) => typeof v === 'string')) {
    return { error: 'variables: must be an object of strings' };
  }
  return { req };
}

/** Runs `fn(span)` in an active child span; a throw records the exception and sets ERROR. */
function inSpan(name, fn) {
  return tracer.startActiveSpan(name, async (span) => {
    try {
      return await fn(span);
    } catch (err) {
      recordError(span, err);
      throw err;
    } finally {
      span.end();
    }
  });
}

// --- App ---
const app = express();
app.use(express.json());

// /health has no server span (the helper's health sampler) but still counts in
// http.server.request.duration.
app.get('/health', (_req, res) => {
  res.json({ status: 'ok' });
});

app.post('/notify', async (req, res) => {
  const parsed = parseNotify(req.body);
  if (parsed.error) {
    res.status(422).json({ detail: parsed.error });
    return;
  }
  const n = parsed.req;
  activeRequests.add(1);
  try {
    await inSpan('api.send-notification', async (span) => {
      span.setAttribute('notification.channel', n.channel);
      // The examples' recipients are synthetic (user-NNNN@example.com). A real
      // address is personal data: do not put it in span attributes.
      span.setAttribute('notification.recipient', n.recipient);
      span.setAttribute('notification.template', n.template_id);

      const reply = await callBackend('SendNotification', n);

      notificationsDb.set(reply.notification_id, {
        id: reply.notification_id,
        status: reply.status,
        recipient: n.recipient,
        channel: n.channel,
        created_at: Date.now() / 1000,
      });

      if (reply.status === 'failed') {
        notificationsFailed.add(1, { channel: n.channel });
        span.setStatus({ code: SpanStatusCode.ERROR, message: 'Backend reported failure' });
      } else {
        notificationsSent.add(1, { channel: n.channel });
      }

      logger.info('Notification sent', { notification_id: reply.notification_id, status: reply.status });
      res.json({ notification_id: reply.notification_id, status: reply.status });
    });
  } finally {
    activeRequests.add(-1);
  }
});

app.get('/notify/:notificationId', async (req, res) => {
  const { notificationId } = req.params;
  await inSpan('api.get-status', async (span) => {
    span.setAttribute('notification.id', notificationId);
    const local = notificationsDb.get(notificationId);
    if (local) {
      res.json(local);
      return;
    }
    // Ask the backend.
    const reply = await callBackend('GetStatus', { notification_id: notificationId });
    res.json({ id: reply.notification_id, status: reply.status, delivered_at: reply.delivered_at || null });
  });
});

app.get('/templates', (_req, res) => {
  res.json([...templatesDb.values()]);
});

app.post('/templates', (req, res) => {
  const b = req.body;
  if (!isPlainObject(b) || typeof b.name !== 'string' || typeof b.body !== 'string' || typeof b.subject !== 'string') {
    res.status(422).json({ detail: 'name, body and subject: field required (string)' });
    return;
  }
  tracer.startActiveSpan('api.create-template', (span) => {
    span.setAttribute('template.name', b.name);
    templatesDb.set(b.name, { name: b.name, body: b.body, subject: b.subject });
    templateRenders.add(1);
    span.end();
  });
  res.json({ status: 'created', name: b.name });
});

app.get('/metrics/summary', (_req, res) => {
  const all = [...notificationsDb.values()];
  res.json({
    total_notifications: all.length,
    templates_available: templatesDb.size,
    channels: [...new Set(all.map((n) => n.channel))],
  });
});

// Unknown route: JSON, as FastAPI's {"detail": "Not Found"}.
app.use((_req, res) => {
  res.status(404).json({ detail: 'Not Found' });
});

// Final error handler (express needs all four parameters): a malformed JSON
// body is a 400 (body-parser sets `status`), anything else a 500 with a JSON body.
app.use((err, _req, res, next) => {
  const status = Number.isInteger(err?.status) && err.status >= 400 && err.status < 500 ? err.status : 500;
  if (status === 500) logger.error('Request failed', errorAttributes(err));
  // A response already started: let express close the connection.
  if (res.headersSent) return next(err);
  // A fixed text per status, never the error's own message (it can quote the body).
  res.status(status).json({ detail: http.STATUS_CODES[status] ?? 'Error' });
});

// The helper installs no signal handler: on SIGTERM (docker stop, a rollout)
// stop accepting requests, then flush buffered telemetry. 4 s drain + 5 s
// flush stay inside docker stop's default 10 s grace period.
let stopping = false;
function stop(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  const drained = new Promise((resolve) => {
    server.close(() => resolve());
    setTimeout(resolve, 4_000).unref();
  });
  drained
    .then(async () => {
      backend.close();
      // Let handlers that were still finishing record their spans and logs.
      await new Promise((resolve) => setTimeout(resolve, 100));
      return shutdown(5_000);
    })
    .catch((err) => { console.error('telemetry shutdown incomplete:', err); })
    .finally(() => process.exit(exitCode));
}

const server = app.listen(PORT, '0.0.0.0', (err) => {
  if (err) return; // reported by the 'error' listener below
  logger.info(`Notification API listening on :${PORT}`);
});
server.on('error', (err) => {
  logger.error('HTTP listen failed', errorAttributes(err));
  stop(1);
});
process.once('SIGTERM', () => stop());
process.once('SIGINT', () => stop());
