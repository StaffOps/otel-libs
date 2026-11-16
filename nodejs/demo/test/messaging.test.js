'use strict';

const assert = require('node:assert/strict');
const { after, before, test } = require('node:test');
const { ROOT_CONTEXT, SpanKind, TraceFlags, propagation, trace } = require('@opentelemetry/api');

const { CONSUMER_GROUP, consumerSpan, extractContext, injectCarrier, producerSpan, quoteKey, streamKey } = require('../lib/messaging');

// A minimal traceparent propagator (the real one is the helper's W3C global).
const testPropagator = {
  inject(ctx, carrier, setter) {
    const sc = trace.getSpanContext(ctx);
    if (sc === undefined) return;
    setter.set(carrier, 'traceparent', `00-${sc.traceId}-${sc.spanId}-0${sc.traceFlags}`);
    setter.set(carrier, 'numeric', 7); // a non-string value is dropped by injectCarrier
  },
  extract(ctx, carrier, getter) {
    const value = getter.get(carrier, 'traceparent');
    const m = /^00-([0-9a-f]{32})-([0-9a-f]{16})-0(\d)$/.exec(typeof value === 'string' ? value : '');
    if (m === null) return ctx;
    return trace.setSpanContext(ctx, { traceId: m[1], spanId: m[2], traceFlags: Number(m[3]), isRemote: true });
  },
  fields: () => ['traceparent'],
};

before(() => propagation.setGlobalPropagator(testPropagator));
after(() => propagation.disable());

const SERVER = { host: 'demo-redis', port: 6379 };
const PRODUCER_CTX = trace.setSpanContext(ROOT_CONTEXT, {
  traceId: '0af7651916cd43dd8448eb211c80319c',
  spanId: 'b7ad6b7169203331',
  traceFlags: TraceFlags.SAMPLED,
});

test('keys follow DE-D4', () => {
  assert.equal(streamKey('node'), 'demo:node:orders');
  assert.equal(quoteKey('node', 'cust-a'), 'demo:node:quote:cust-a');
  assert.equal(CONSUMER_GROUP, 'demo-workers');
});

test('producerSpan: name, PRODUCER kind and DE-D6 attributes', () => {
  const { name, options } = producerSpan('demo:node:orders', SERVER);
  assert.equal(name, 'send demo:node:orders');
  assert.equal(options.kind, SpanKind.PRODUCER);
  assert.deepEqual(options.attributes, {
    'messaging.system': 'redis',
    'messaging.operation.name': 'send',
    'messaging.operation.type': 'send',
    'messaging.destination.name': 'demo:node:orders',
    'server.address': 'demo-redis',
    'server.port': 6379,
  });
});

test('injectCarrier writes the string fields of the global propagator', () => {
  assert.deepEqual(injectCarrier(PRODUCER_CTX), { traceparent: '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01' });
  assert.deepEqual(injectCarrier(ROOT_CONTEXT), {});
  assert.deepEqual(injectCarrier(), {}); // the active context: root, no span
});

test('extractContext reads string fields only', () => {
  const ctx = extractContext({ traceparent: '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01', other: Buffer.from('x') });
  assert.equal(trace.getSpanContext(ctx).spanId, 'b7ad6b7169203331');
  assert.equal(trace.getSpanContext(extractContext(undefined)), undefined);
});

test('consumerSpan: one link to the producer and the producer as parent', () => {
  const fields = injectCarrier(PRODUCER_CTX);
  const { name, parent, options } = consumerSpan('demo:node:orders', SERVER, '1-0', fields);
  assert.equal(name, 'process demo:node:orders');
  assert.equal(options.kind, SpanKind.CONSUMER);
  assert.equal(options.links.length, 1);
  assert.equal(options.links[0].context.spanId, 'b7ad6b7169203331');
  assert.equal(options.links[0].context.traceId, '0af7651916cd43dd8448eb211c80319c');
  assert.equal(trace.getSpanContext(parent).spanId, 'b7ad6b7169203331');
  assert.deepEqual(options.attributes, {
    'messaging.system': 'redis',
    'messaging.operation.name': 'process',
    'messaging.operation.type': 'process',
    'messaging.destination.name': 'demo:node:orders',
    'server.address': 'demo-redis',
    'server.port': 6379,
    'messaging.message.id': '1-0',
    'messaging.consumer.group.name': 'demo-workers',
  });
});

test('consumerSpan without a valid context: no link, root parent', () => {
  const { parent, options } = consumerSpan('demo:node:orders', SERVER, '2-0', { traceparent: 'garbage' });
  assert.deepEqual(options.links, []);
  assert.equal(parent, ROOT_CONTEXT);
  const zero = consumerSpan('s', SERVER, '3-0', { traceparent: `00-${'0'.repeat(32)}-${'0'.repeat(16)}-01` });
  assert.deepEqual(zero.options.links, []);
});
