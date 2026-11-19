// Custom signals with @staffops/otel-helper: spans, metrics and logs correlated
// with the active trace.
//
// A short-lived job that processes one batch of three simulated orders (ord-3
// fails) and exits. It shows the three custom-signal APIs an application uses
// on top of the helper's automatic instrumentation, with the same names and
// trace shape as the Go and Python twins (go/example/go-custom-signals,
// python/example/python-custom-signals):
//
// - spans: a batch root span (startRootSpan) with one child span per order,
//   attributes, an event, status and exception recording;
// - metrics: counter, histogram, up-down counter and observable gauge, each
//   with attributes;
// - logs: createLogger() records, exported with the trace_id/span_id of the
//   span that is active when the record is emitted.
//
// Run it against a Collector (see ../README.md, "Custom signals"):
//
//   OTEL_SERVICE_NAME=node-custom-signals \
//   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
//   node custom-signals.js
//
// Without an endpoint, logs go to stdout as JSON and metrics to the Prometheus
// /metrics listener (gone when the process exits).
'use strict';

const { createLogger, getMeter, getTracer, setup, shutdown, startRootSpan } = require('@staffops/otel-helper');

setup();

const { setTimeout: sleep } = require('node:timers/promises');
const { SpanStatusCode, ValueType } = require('@opentelemetry/api');

/** Instrumentation scope name of the tracer and the meter (as Go and Python). */
const SCOPE = 'orders';

const tracer = getTracer(SCOPE);
const meter = getMeter(SCOPE);
// The logger name is the log records' instrumentation scope (Node examples:
// the app name; Go writes `otel-helper`, Python the logging logger name).
const logger = createLogger({ name: 'node-custom-signals' });

/** Business error of a failed charge, recorded on the span and logged. */
class PaymentDeclinedError extends Error {
  constructor(message) {
    super(message);
    // exception.type is the error's name: set it, or it reads `Error`. An
    // error with a `code` (an errno string, a gRPC status number) gets the
    // code as exception.type instead: record such errors as a plain
    // { name, message, stack }, as node-process's recordError() does.
    this.name = 'PaymentDeclinedError';
  }
}

const ORDERS = Object.freeze([
  { id: 'ord-1', kind: 'standard', amount: 42.5 },
  { id: 'ord-2', kind: 'express', amount: 99.9 },
  { id: 'ord-3', kind: 'standard', amount: 10.5, fail: true },
]);

// State read by the observable gauge callback (the SDK calls it at every
// collection: export interval, scrape, shutdown).
let queueDepth = ORDERS.length;

// Set by SIGTERM / SIGINT: the loop stops taking new orders, the flush still runs.
let stopRequested = false;

// Instrument names follow OTel conventions: dot-separated, unit in `unit`, no
// `_total` suffix (the Prometheus exporter adds `_total` to counters). Counts
// are integers (`ValueType.INT`, as Go's Int64 instruments and Python's int
// adds); without it the SDK exports a JavaScript number as a double.
const processed = meter.createCounter('order.processed', {
  valueType: ValueType.INT,
  unit: '{order}',
  description: 'Orders processed',
});
// Second-based histograms need their own boundaries: the SDK defaults
// (0, 5, 10, ... 10000) suit milliseconds.
const duration = meter.createHistogram('order.duration', {
  unit: 's',
  description: 'Time spent processing one order',
  advice: { explicitBucketBoundaries: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1] },
});
const active = meter.createUpDownCounter('order.active', {
  valueType: ValueType.INT,
  unit: '{order}',
  description: 'Orders being processed now',
});
const queueDepthGauge = meter.createObservableGauge('order.queue.depth', {
  valueType: ValueType.INT,
  unit: '{order}',
  description: 'Orders waiting in the queue',
});
queueDepthGauge.addCallback((result) => {
  result.observe(queueDepth, { 'queue.name': 'orders' });
});

function charge(order) {
  if (order.fail) throw new PaymentDeclinedError('card declined by issuer');
}

/** Processes one order in a child span of the batch span (the active span); true when it failed. */
function processOrder(order) {
  return tracer.startActiveSpan('order.process', {
    // order.id is fine on spans; never as a metric attribute.
    attributes: { 'order.id': order.id, 'order.type': order.kind },
  }, async (span) => {
    const started = performance.now();
    const typeAttrs = { 'order.type': order.kind };
    let attrs = typeAttrs;
    let failed = false;
    active.add(1, typeAttrs);
    try {
      // An event is a timestamped annotation inside the span.
      span.addEvent('order.validated', { 'order.amount': order.amount });
      await sleep(20); // simulated call to a payment provider
      try {
        charge(order);
        logger.info('order processed', { 'order.id': order.id, 'order.amount': order.amount });
      } catch (err) {
        // startActiveSpan records nothing for a thrown error: an unexpected
        // one propagates and is recorded only on the root (startRootSpan).
        if (!(err instanceof PaymentDeclinedError)) throw err;
        // Handled error: record it explicitly (exception event + ERROR).
        span.recordException(err);
        span.setStatus({ code: SpanStatusCode.ERROR, message: 'payment declined' });
        // error.type only on failure, low-cardinality: the error's name.
        attrs = { ...attrs, 'error.type': err.name };
        failed = true;
        // Correlated with order.process (the active span); an Error-valued
        // attribute becomes the record's exception.type / exception.message /
        // exception.stacktrace.
        logger.error('order failed', { 'order.id': order.id, error: err });
      }
    } finally {
      active.add(-1, typeAttrs);
      duration.record((performance.now() - started) / 1000, attrs);
      processed.add(1, attrs);
      queueDepth -= 1;
      span.end();
    }
    return failed;
  });
}

async function main() {
  // A root span starts a new trace, whatever span is active (workers,
  // consumers, cron jobs: one trace per unit of work).
  await startRootSpan('orders.process-batch', async (batch) => {
    process.stdout.write(`trace_id=${batch.spanContext().traceId}\n`);

    let failed = 0;
    for (const order of ORDERS) {
      if (stopRequested) {
        logger.warn('batch interrupted', { 'orders.pending': queueDepth });
        break;
      }
      if (await processOrder(order)) failed += 1;
    }

    if (failed > 0) {
      // Status only: the error itself was recorded on the child span.
      batch.setStatus({ code: SpanStatusCode.ERROR, message: `${failed} of ${ORDERS.length} orders failed` });
    }
    // Correlated with orders.process-batch (the active span again).
    logger.info('batch finished', { 'batch.size': ORDERS.length, 'orders.failed': failed });
  }, { tracer, attributes: { 'batch.size': ORDERS.length } });
}

// The helper installs no signal handler: SIGTERM/SIGINT become a stop
// request, so the flush below still runs. The handlers are `once` on
// purpose, as in node-process: a second signal gets Node's default action
// and force-quits without the flush.
process.once('SIGTERM', () => { stopRequested = true; });
process.once('SIGINT', () => { stopRequested = true; });

main()
  .catch((err) => {
    console.error('node-custom-signals:', err);
    process.exitCode = 1;
  })
  // Flushes the batch span/log processors and exports the last metric interval.
  .finally(() => shutdown(5_000)
    .catch((err) => console.error('telemetry shutdown incomplete:', err))
    // The deadline bounds the promise, not the process: exit explicitly.
    .finally(() => process.exit()));
