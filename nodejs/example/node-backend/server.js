// Notification backend: a gRPC server that processes notification delivery.
// Node twin of python-backend.
'use strict';

// The helper first: @grpc/grpc-js must be loaded after setup() so the server
// is instrumented.
const { createLogger, getMeter, getTracer, setup, shutdown } = require('@staffops/otel-helper');

setup({ resourceAttributes: { 'app.component': 'notification-processor' } });

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { setTimeout: sleep } = require('node:timers/promises');
const { SpanStatusCode, ValueType } = require('@opentelemetry/api');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');
const { HealthImplementation } = require('grpc-health-check');

const tracer = getTracer('node-backend');
const meter = getMeter('node-backend');

// Recorded in seconds: the SDK's default buckets (0..10000) are meant for
// milliseconds. Legacy names, kept identical to python-backend.
const deliveryDuration = meter.createHistogram('notification.delivery_duration_seconds', {
  advice: { explicitBucketBoundaries: [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5] },
});
// Integer counts (ValueType.INT), as Python's int adds; the JS default is a double (design D-JS14).
const deliveryAttempts = meter.createCounter('notification.delivery_attempts_total', { valueType: ValueType.INT });

const logger = createLogger({ name: 'node-backend' });

const PORT = Number(process.env.PORT ?? 50051);
// NOTIFICATION_PROTO, else ./protos (the image layout), else ../protos (a source checkout).
const PROTO_PATH = process.env.NOTIFICATION_PROTO
  ?? [path.join(__dirname, 'protos', 'notification.proto'), path.join(__dirname, '..', 'protos', 'notification.proto')]
    .find((p) => fs.existsSync(p))
  ?? path.join(__dirname, 'protos', 'notification.proto');
const { notification } = grpc.loadPackageDefinition(
  protoLoader.loadSync(PROTO_PATH, { keepCase: true, longs: String, enums: String, defaults: true }),
);

// --- In-memory state ---
const notificationsStore = new Map();

const TEMPLATES = {
  default: { body: 'Hello {name}', subject: 'Notification' },
  welcome: { body: 'Welcome {name} to {product}', subject: 'Welcome!' },
  alert: { body: 'Alert: {message}', subject: '⚠️ Alert' },
};

/** Fills `{key}` placeholders; a missing variable leaves the body unrendered (as Python's str.format KeyError path). */
function render(body, variables) {
  const keys = [...body.matchAll(/\{(\w+)\}/g)].map((m) => m[1]);
  if (keys.some((k) => !Object.hasOwn(variables, k))) return body;
  return body.replace(/\{(\w+)\}/g, (_, k) => variables[k]);
}

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

const uniform = (min, max) => min + Math.random() * (max - min);

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

/** Adapts an async handler to grpc-js's callback API; a rejection becomes UNKNOWN, as an uncaught exception in grpcio. */
function unary(handler) {
  return (call, callback) => {
    handler(call.request).then(
      (reply) => callback(null, reply),
      (err) => callback({ code: grpc.status.UNKNOWN, details: err instanceof Error ? err.message : String(err) }),
    );
  };
}

const service = {
  // Simulates sending a notification (email/sms/push).
  SendNotification: unary((request) => inSpan('backend.send-notification', async (span) => {
    const notificationId = crypto.randomUUID().slice(0, 8);
    span.setAttribute('notification.id', notificationId);
    span.setAttribute('notification.channel', request.channel);
    // Synthetic recipients in the examples; a real address is personal data.
    span.setAttribute('notification.recipient', request.recipient);

    // Simulated template rendering.
    const body = await inSpan('backend.render-template', async () => {
      const template = TEMPLATES[request.template_id] ?? TEMPLATES.default;
      const rendered = render(template.body, request.variables);
      await sleep(uniform(5, 20));
      return rendered;
    });

    // Simulated delivery (external provider call).
    const status = await inSpan('backend.deliver', async (deliverSpan) => {
      deliverSpan.setAttribute('notification.provider', `${request.channel}-provider`);
      const start = performance.now();
      const delay = { email: 0.1, sms: 0.05, push: 0.02 }[request.channel] ?? 0.1;
      await sleep(uniform(delay * 0.5, delay * 1.5) * 1000);

      // Occasional failures (10%).
      const failed = Math.random() < 0.1;
      const duration = (performance.now() - start) / 1000;
      deliveryDuration.record(duration, { channel: request.channel });
      deliveryAttempts.add(1, { channel: request.channel, success: failed ? 'False' : 'True' });

      if (failed) {
        deliverSpan.setStatus({ code: SpanStatusCode.ERROR, message: 'Provider timeout' });
        return 'failed';
      }
      return 'sent';
    });

    notificationsStore.set(notificationId, {
      id: notificationId,
      status,
      recipient: request.recipient,
      channel: request.channel,
      body,
      delivered_at: status === 'sent' ? Date.now() / 1000 : null,
    });

    logger.info('Notification processed', { notification_id: notificationId, status, channel: request.channel });
    return { notification_id: notificationId, status };
  })),

  // Checks the delivery status of a notification.
  GetStatus: unary((request) => inSpan('backend.get-status', async (span) => {
    span.setAttribute('notification.id', request.notification_id);
    const record = notificationsStore.get(request.notification_id);
    if (!record) return { notification_id: request.notification_id, status: 'not_found' };
    return {
      notification_id: record.id,
      status: record.status,
      delivered_at: record.delivered_at ? String(record.delivered_at) : '',
    };
  })),

  // Renders a template with variables.
  RenderTemplate: unary((request) => inSpan('backend.render-template', async (span) => {
    span.setAttribute('template.id', request.template_id);
    const template = TEMPLATES[request.template_id] ?? TEMPLATES.default;
    return { rendered_body: render(template.body, request.variables), subject: template.subject };
  })),
};

const server = new grpc.Server();
server.addService(notification.NotificationService.service, service);

// Standard grpc.health.v1 service; the helper's health sampler drops its spans.
const health = new HealthImplementation({ '': 'SERVING' });
health.addToServer(server);

// The helper installs no signal handler. Registered before the bind, so a
// signal that arrives during startup is not lost. 4 s drain + 5 s flush stay
// inside docker stop's default 10 s grace period.
let stopping = false;
function stop() {
  if (stopping) return;
  stopping = true;
  // Tell health checkers first, so load balancers stop sending new calls.
  health.setStatus('', 'NOT_SERVING');
  const drained = new Promise((resolve) => {
    server.tryShutdown(() => resolve());
    setTimeout(() => { server.forceShutdown(); resolve(); }, 4_000).unref();
  });
  drained
    .then(() => shutdown(5_000))
    .catch((err) => { console.error('telemetry shutdown incomplete:', err); })
    .finally(() => process.exit(0));
}
process.once('SIGTERM', stop);
process.once('SIGINT', stop);

server.bindAsync(`0.0.0.0:${PORT}`, grpc.ServerCredentials.createInsecure(), (err) => {
  if (err) {
    logger.error('gRPC bind failed', errorAttributes(err));
    process.exitCode = 1;
    shutdown(5_000).finally(() => process.exit(1));
    return;
  }
  logger.info(`Backend gRPC server starting on :${PORT}`);
});
