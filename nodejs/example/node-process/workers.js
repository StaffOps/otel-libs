// Notification process: background workers for queue consumption, retries,
// cleanup and API health checks. Node twin of python-process.
'use strict';

// The helper first: node:http and fetch (undici) are instrumented by setup().
const { createLogger, getMeter, getTracer, setup, shutdown, startRootSpan } = require('@staffops/otel-helper');

setup({ resourceAttributes: { 'app.component': 'notification-workers' } });

const http = require('node:http');
const { setTimeout: sleep } = require('node:timers/promises');
const { SpanStatusCode } = require('@opentelemetry/api');

const logger = createLogger({ name: 'node-process' });
const tracer = getTracer('node-process');
const meter = getMeter('node-process');

// Legacy instrument names, kept identical to python-process.
const queueDepth = meter.createUpDownCounter('notifications.queue_depth');
const retriesTotal = meter.createCounter('notifications.retries_total');
const cleanupTotal = meter.createCounter('notifications.cleaned_total');

const API_URL = process.env.API_URL ?? 'http://node-api-stable:8000';
const HEALTH_PORT = Number(process.env.HEALTH_PORT ?? 8080);
/** Per-request timeout; with the 4 s drain below, SIGTERM never waits on a stuck API. */
const REQUEST_TIMEOUT_MS = 5_000;
const DRAIN_MS = 4_000;

// No manual traceparent injection: the helper's fetch (undici) instrumentation
// injects the client span's context and *appends* the header, so a
// hand-injected traceparent would be sent twice and the server would start a
// new trace (design D-JS14).

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

/** A fetch failure's reason: undici's `TypeError: fetch failed` carries the network error as `cause`. */
function fetchFailure(err) {
  const { cause } = err instanceof Error ? err : {};
  return describeError(cause === undefined ? err : cause).message;
}

/** Runs `fn(span)` in an active child span of the current one; a throw records it and sets ERROR. */
function withSpan(name, fn) {
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

const randomInt = (min, max) => min + Math.floor(Math.random() * (max - min + 1));
const choice = (items) => items[Math.floor(Math.random() * items.length)];

/** Aborted by shutdown or after REQUEST_TIMEOUT_MS, whichever comes first. */
const requestSignal = (signal) => AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]);

function postNotify(body, signal) {
  return fetch(`${API_URL}/notify`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    signal: requestSignal(signal),
  });
}

/** Runs `cycle()` every `periodMs` until `signal` aborts; a cycle's error is logged, not fatal. */
async function every(periodMs, signal, cycle) {
  while (!signal.aborted) {
    try {
      await cycle();
    } catch (err) {
      if (signal.aborted) return; // stopping: an aborted cycle is not an error
      logger.error('Worker cycle failed', errorAttributes(err));
    }
    try {
      await sleep(periodMs, undefined, { signal });
    } catch {
      return; // aborted
    }
  }
}

// Consumes pending notifications from a (simulated) queue.
function queueConsumer(signal) {
  return every(30_000, signal, () => startRootSpan('process.queue-consume', async (span) => {
    const batchSize = randomInt(1, 5);
    span.setAttribute('queue.batch_size', batchSize);
    queueDepth.add(batchSize);
    try {
      for (let i = 0; i < batchSize; i++) {
        if (signal.aborted) break; // stopping: leave the rest of the batch
        await withSpan('process.dispatch-notification', async (itemSpan) => {
          // Synthetic recipients; a real address is personal data — keep it out of spans.
          const recipient = `user-${randomInt(1000, 9999)}@example.com`;
          const channel = choice(['email', 'sms', 'push']);
          itemSpan.setAttribute('notification.recipient', recipient);
          itemSpan.setAttribute('notification.channel', channel);
          try {
            const response = await postNotify({
              recipient,
              channel,
              template_id: choice(['default', 'welcome', 'alert']),
              variables: { name: `User${i}`, message: 'System check' },
            }, signal);
            await response.arrayBuffer();
            if (response.status !== 200) {
              itemSpan.setStatus({ code: SpanStatusCode.ERROR, message: `HTTP ${response.status}` });
            }
          } catch (err) {
            if (signal.aborted) return; // stopping: the request was aborted, not failed
            itemSpan.setStatus({ code: SpanStatusCode.ERROR, message: fetchFailure(err) });
            logger.error('Failed to dispatch', { error: fetchFailure(err) });
          }
        });
      }
    } finally {
      queueDepth.add(-batchSize);
    }
  }, { tracer }));
}

// Retries failed notifications.
function retryWorker(signal) {
  return every(60_000, signal, () => startRootSpan('process.retry-cycle', async (span) => {
    const failedCount = randomInt(0, 3);
    span.setAttribute('retry.failed_count', failedCount);
    for (let i = 0; i < failedCount; i++) {
      if (signal.aborted) break; // stopping
      await withSpan('process.retry-attempt', async (retrySpan) => {
        retrySpan.setAttribute('retry.attempt', i + 1);
        retriesTotal.add(1);
        try {
          const response = await postNotify({
            recipient: `retry-${randomInt(100, 999)}@example.com`,
            channel: 'email',
            template_id: 'alert',
            variables: { message: 'Retry attempt' },
          }, signal);
          await response.arrayBuffer();
        } catch (err) {
          if (signal.aborted) return; // stopping
          retrySpan.setStatus({ code: SpanStatusCode.ERROR, message: fetchFailure(err) });
        }
      });
    }
    logger.info('Retry cycle complete', { retried: failedCount });
  }, { tracer }));
}

// Cleans up expired notifications (older than 24 h, simulated).
function cleanupWorker(signal) {
  return every(180_000, signal, () => startRootSpan('process.cleanup', async (span) => {
    const cleaned = randomInt(0, 10);
    span.setAttribute('cleanup.removed', cleaned);
    cleanupTotal.add(cleaned);
    if (cleaned > 0) logger.info('Cleaned expired notifications', { count: cleaned });
  }, { tracer }));
}

// Periodically checks the API's health.
function healthChecker(signal) {
  return every(60_000, signal, () => startRootSpan('process.health-check', async (span) => {
    try {
      const response = await fetch(`${API_URL}/health`, { signal: requestSignal(signal) });
      await response.arrayBuffer();
      span.setAttribute('health.status_code', response.status);
      if (response.status !== 200) span.setStatus({ code: SpanStatusCode.ERROR, message: 'API unhealthy' });
    } catch (err) {
      if (signal.aborted) return; // stopping
      span.setStatus({ code: SpanStatusCode.ERROR, message: fetchFailure(err) });
      logger.warn('Health check failed', { error: fetchFailure(err) });
    }
  }, { tracer }));
}

// --- Health server (liveness/readiness probes) ---
// A node:http server, so the helper instruments it (python-process's aiohttp
// probe server is not): /health and /healthz record no span but count in
// http.server.request.duration (design D-JS14).
const healthServer = http.createServer((req, res) => {
  if (req.method === 'GET' && (req.url === '/health' || req.url === '/healthz')) {
    res.writeHead(200, { 'content-type': 'text/plain' }).end('ok');
    return;
  }
  res.writeHead(404).end();
});

async function main() {
  // The helper installs no signal handler: SIGTERM/SIGINT abort the workers
  // and their requests; the drain is bounded (4 s), then shutdown() flushes
  // (5 s) — inside docker stop's default 10 s grace period.
  const controller = new AbortController();
  const { signal } = controller;
  process.once('SIGTERM', () => controller.abort());
  process.once('SIGINT', () => controller.abort());
  const drainLimit = new Promise((resolve) => {
    signal.addEventListener('abort', () => setTimeout(resolve, DRAIN_MS).unref(), { once: true });
  });

  logger.info('Starting notification workers');
  await new Promise((resolve, reject) => {
    healthServer.once('error', reject);
    healthServer.listen(HEALTH_PORT, '0.0.0.0', resolve);
  });
  await Promise.race([
    Promise.all([queueConsumer(signal), retryWorker(signal), cleanupWorker(signal), healthChecker(signal)]),
    drainLimit,
  ]);
  healthServer.close();
  logger.info('Notification workers stopped');
}

main()
  .catch((err) => {
    console.error('node-process:', err);
    process.exitCode = 1;
  })
  .finally(() => shutdown(5_000)
    .catch((err) => console.error('telemetry shutdown incomplete:', err))
    // The deadline bounds the promise, not the process: exit explicitly.
    .finally(() => process.exit()));
