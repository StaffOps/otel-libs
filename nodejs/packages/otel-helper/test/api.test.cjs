'use strict';
// getTracer / getMeter / startRootSpan (B11b; design "Layout and public API"):
// the Node counterparts of Go GetTracer/GetMeter/StartRootSpan
// (go/otelhelper.go, go/tracing.go) and Python get_tracer/get_meter/
// start_root_span (staffops_otel_helper/__init__.py, tracing.py).
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('@opentelemetry/api');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');

let exporter;
let saved;

test.beforeEach(() => {
  saved = { ...process.env };
  for (const k of Object.keys(process.env)) {
    if (k.startsWith('OTEL_') || k === 'SERVICE_NAME' || k === 'ENVIRONMENT') delete process.env[k];
  }
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  process.env.OTEL_METRICS_EXPORTER = 'none';
  process.env.OTEL_LOGS_EXPORTER = 'none';
  testing.reset();
  testing.setDiagnosticSink(() => undefined);
  exporter = new InMemorySpanExporter();
  helper.setup({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
});

test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

const spans = () => exporter.getFinishedSpans();
const byName = (name) => spans().find((s) => s.name === name);

test('getTracer/getMeter: default scope otel-helper, empty name too, explicit name kept', () => {
  helper.getTracer().startSpan('a').end();
  helper.getTracer('').startSpan('b').end();
  helper.getTracer('my-lib', '1.2.3').startSpan('c').end();
  assert.equal(byName('a').instrumentationScope.name, 'otel-helper');
  assert.equal(byName('b').instrumentationScope.name, 'otel-helper');
  assert.deepEqual([byName('c').instrumentationScope.name, byName('c').instrumentationScope.version], ['my-lib', '1.2.3']);

  // The meters come from the global MeterProvider: same object as the API's for the same scope.
  assert.equal(helper.getMeter(), api.metrics.getMeter('otel-helper'));
  assert.equal(helper.getMeter(''), api.metrics.getMeter('otel-helper'));
  assert.equal(helper.getMeter('my-lib'), api.metrics.getMeter('my-lib'));
});

test('startRootSpan sync: returns fn result, span active inside, ended, INTERNAL, default scope, attributes at start', () => {
  let inside;
  const out = helper.startRootSpan('cycle', (span) => {
    inside = api.trace.getActiveSpan();
    assert.equal(span, inside);
    return 42;
  }, { attributes: { 'batch.size': 3 } });
  assert.equal(out, 42);
  const s = byName('cycle');
  assert.ok(s, 'span exported (ended)');
  assert.equal(s.kind, api.SpanKind.INTERNAL);
  assert.equal(s.instrumentationScope.name, 'otel-helper');
  assert.equal(s.parentSpanContext, undefined);
  assert.deepEqual(s.attributes, { 'batch.size': 3 });
  assert.equal(s.status.code, api.SpanStatusCode.UNSET);
  assert.equal(api.trace.getActiveSpan(), undefined, 'context restored after fn');
});

test('startRootSpan async: ended only when the promise settles, child spans share its trace', async () => {
  const tracer = helper.getTracer('worker');
  const p = helper.startRootSpan('cycle', async () => {
    await new Promise((r) => setTimeout(r, 5));
    assert.equal(byName('cycle'), undefined, 'not ended before the promise settles');
    tracer.startActiveSpan('child', (c) => c.end());
    return 'done';
  }, { tracer });
  assert.ok(p instanceof Promise);
  assert.equal(await p, 'done');
  const root = byName('cycle');
  const child = byName('child');
  assert.equal(root.instrumentationScope.name, 'worker', 'custom tracer used');
  assert.equal(child.spanContext().traceId, root.spanContext().traceId);
  assert.equal(child.parentSpanContext.spanId, root.spanContext().spanId);
});

test('startRootSpan inside an active span starts a new trace, keeps baggage, restores the outer span', () => {
  const tracer = helper.getTracer();
  const bag = api.propagation.createBaggage({ tenant: { value: 't1' } });
  api.context.with(api.propagation.setBaggage(api.context.active(), bag), () => {
    tracer.startActiveSpan('outer', (outer) => {
      helper.startRootSpan('root', () => {
        assert.equal(api.propagation.getBaggage(api.context.active())?.getEntry('tenant')?.value, 't1');
      });
      assert.equal(api.trace.getActiveSpan(), outer);
      outer.end();
    });
  });
  const outer = byName('outer');
  const root = byName('root');
  assert.notEqual(root.spanContext().traceId, outer.spanContext().traceId);
  assert.equal(root.parentSpanContext, undefined);
});

test('startRootSpan with a remote parent active: still a new trace', () => {
  const remote = api.trace.setSpanContext(api.context.active(), {
    traceId: '0af7651916cd43dd8448eb211c80319c', spanId: 'b7ad6b7169203331', traceFlags: 1, isRemote: true,
  });
  api.context.with(remote, () => helper.startRootSpan('root', () => undefined));
  assert.notEqual(byName('root').spanContext().traceId, '0af7651916cd43dd8448eb211c80319c');
});

test('startRootSpan sync throw: exception event, ERROR "<name>: <message>", ended, rethrown', () => {
  class PaymentDeclinedError extends Error {
    constructor(m) { super(m); this.name = 'PaymentDeclinedError'; }
  }
  assert.throws(() => helper.startRootSpan('fail', () => { throw new PaymentDeclinedError('declined'); }), PaymentDeclinedError);
  const s = byName('fail');
  assert.deepEqual(s.status, { code: api.SpanStatusCode.ERROR, message: 'PaymentDeclinedError: declined' });
  const ev = s.events.find((e) => e.name === 'exception');
  assert.equal(ev.attributes['exception.type'], 'PaymentDeclinedError');
  assert.equal(typeof ev.attributes['exception.stacktrace'], 'string');
  assert.equal(ev.attributes['exception.message'], 'declined');
});

test('startRootSpan async reject: exception event, ERROR, ended, rejection propagated', async () => {
  await assert.rejects(helper.startRootSpan('fail', async () => { throw new TypeError('bad'); }), TypeError);
  const s = byName('fail');
  assert.deepEqual(s.status, { code: api.SpanStatusCode.ERROR, message: 'TypeError: bad' });
  assert.ok(s.events.some((e) => e.name === 'exception'));
});

test('startRootSpan non-Error throw and reject: status message is the value', async () => {
  assert.throws(() => helper.startRootSpan('s', () => { throw 'boom'; }), (e) => e === 'boom');
  await assert.rejects(helper.startRootSpan('a', () => Promise.reject(7)), (e) => e === 7);
  assert.deepEqual(byName('s').status, { code: api.SpanStatusCode.ERROR, message: 'boom' });
  assert.deepEqual(byName('a').status, { code: api.SpanStatusCode.ERROR, message: '7' });
  assert.ok(byName('s').events.some((e) => e.name === 'exception'));
});

test('startRootSpan: kind and links options, a thenable result is awaited', async () => {
  const link = { context: { traceId: '0af7651916cd43dd8448eb211c80319c', spanId: 'b7ad6b7169203331', traceFlags: 1 } };
  const thenable = { then: (ok) => ok('v') };
  const out = await helper.startRootSpan('k', () => thenable, { kind: api.SpanKind.CONSUMER, links: [link] });
  assert.equal(out, 'v');
  const s = byName('k');
  assert.equal(s.kind, api.SpanKind.CONSUMER);
  assert.equal(s.links.length, 1);
  assert.equal(s.links[0].context.spanId, 'b7ad6b7169203331');
});

test('startRootSpan: the helper debug marker applies (root = entry span)', async () => {
  await helper.shutdown();
  testing.reset();
  testing.setDiagnosticSink(() => undefined);
  exporter = new InMemorySpanExporter();
  helper.setup({ debug: true, spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  api.trace.getTracer('t').startActiveSpan('outer', (outer) => {
    helper.startRootSpan('root', () => undefined);
    outer.end();
  });
  assert.equal(byName('root').attributes.debug, 'true');
});

// --- B11b fix round (2026-11-03) ---

test('startRootSpan: a thenable always gives a real Promise; a throwing then rejects and is recorded', async () => {
  const p = helper.startRootSpan('t1', () => ({ then() { throw new RangeError('then broke'); } }));
  assert.ok(p instanceof Promise);
  await assert.rejects(p, RangeError);
  assert.deepEqual(byName('t1').status, { code: api.SpanStatusCode.ERROR, message: 'RangeError: then broke' });
});

test('startRootSpan: a then that resolves and returns undefined, and a double resolve, settle once', async () => {
  assert.equal(await helper.startRootSpan('t2', () => ({ then(ok) { ok(5); } })), 5);
  assert.equal(await helper.startRootSpan('t3', () => ({ then(ok, fail) { ok(1); ok(2); fail(new Error('late')); } })), 1);
  assert.equal(spans().filter((s) => s.name === 't3').length, 1, 'ended once');
  assert.equal(byName('t3').status.code, api.SpanStatusCode.UNSET);
});

test('startRootSpan: a then getter that throws is a sync failure', () => {
  const bad = Object.defineProperty({}, 'then', { get() { throw new Error('getter'); } });
  assert.throws(() => helper.startRootSpan('t4', () => bad), /getter/);
  assert.deepEqual(byName('t4').status, { code: api.SpanStatusCode.ERROR, message: 'Error: getter' });
});

test('startRootSpan: exception.type is the error name, not its code (gRPC 14, errno)', async () => {
  const grpcErr = Object.assign(new Error('14 UNAVAILABLE: No connection established'), { code: 14 });
  await assert.rejects(helper.startRootSpan('c1', async () => { throw grpcErr; }), (e) => e === grpcErr);
  const ev = byName('c1').events.find((e) => e.name === 'exception');
  assert.equal(ev.attributes['exception.type'], 'Error');
  assert.equal(ev.attributes['exception.message'], '14 UNAVAILABLE: No connection established');
  assert.equal(typeof ev.attributes['exception.stacktrace'], 'string');
});

test('startRootSpan: an Error without a stack string is recorded without a stacktrace', () => {
  const err = new Error('no stack');
  err.stack = undefined;
  assert.throws(() => helper.startRootSpan('c2', () => { throw err; }), (e) => e === err);
  const ev = byName('c2').events.find((e) => e.name === 'exception');
  assert.equal(ev.attributes['exception.stacktrace'], undefined);
});

test('startRootSpan: throw Object.create(null) and a throwing toString rethrow the same value, span ended', () => {
  const bare = Object.create(null);
  assert.throws(() => helper.startRootSpan('n1', () => { throw bare; }), (e) => e === bare);
  assert.deepEqual(byName('n1').status, { code: api.SpanStatusCode.ERROR, message: '[object Object]' });
  const evil = { toString() { throw new Error('no'); } };
  assert.throws(() => helper.startRootSpan('n3', () => { throw evil; }), (e) => e === evil);
  assert.deepEqual(byName('n3').status, { code: api.SpanStatusCode.ERROR, message: '[object Object]' });
  const hostile = new Proxy({}, { getPrototypeOf: () => Object.prototype, get() { throw new Error('trap'); } });
  assert.throws(() => helper.startRootSpan('n2', () => { throw hostile; }), (e) => e === hostile);
  assert.ok(byName('n2'), 'ended');
  assert.deepEqual(byName('n2').status, { code: api.SpanStatusCode.ERROR, message: 'unknown error' });
});

test('startRootSpan: a recording span that throws in recordException still ends and rethrows the original', () => {
  const err = new Error('orig');
  let ended = 0;
  const span = { recordException() { throw new Error('rec'); }, setStatus() {}, end() { ended++; } };
  const tracer = { startActiveSpan: (_n, _o, _c, fn) => fn(span) };
  assert.throws(() => helper.startRootSpan('x', () => { throw err; }, { tracer }), (e) => e === err);
  assert.equal(ended, 1);
});

test('startRootSpan: fails fast on a non-string name or a non-function fn', () => {
  assert.throws(() => helper.startRootSpan(1, () => 1), TypeError);
  assert.throws(() => helper.startRootSpan('x', 'nope'), TypeError);
  assert.equal(spans().length, 0);
});

test('startRootSpan: an unsampled remote parent (traceFlags 0) does not unsample the root', () => {
  const remote = api.trace.setSpanContext(api.context.active(), {
    traceId: '0af7651916cd43dd8448eb211c80319c', spanId: 'b7ad6b7169203331', traceFlags: 0, isRemote: true,
  });
  api.context.with(remote, () => helper.startRootSpan('sampled-root', (span) => {
    assert.equal(span.spanContext().traceFlags & api.TraceFlags.SAMPLED, api.TraceFlags.SAMPLED);
  }));
  assert.ok(byName('sampled-root'), 'exported');
});

test('startRootSpan: the sampler sees no parent and the start attributes', async () => {
  const seen = [];
  const { TracerProvider, InMemorySpanExporter: Mem, SimpleSpanProcessor: Simple } = require('@opentelemetry/sdk-trace');
  const mem = new Mem();
  const provider = new TracerProvider({
    sampler: { shouldSample(ctx, _t, name, _k, attributes) { seen.push({ name, parent: api.trace.getSpanContext(ctx), attributes }); return { decision: 2 }; }, toString: () => 'capture' },
    spanProcessors: [new Simple({ exporter: mem })],
  });
  const tracer = provider.getTracer('cap');
  tracer.startActiveSpan('outer', (outer) => {
    helper.startRootSpan('root', () => undefined, { tracer, attributes: { 'batch.size': 3 } });
    outer.end();
  });
  const root = seen.find((s) => s.name === 'root');
  assert.equal(root.parent, undefined);
  assert.deepEqual(root.attributes, { 'batch.size': 3 });
  await provider.shutdown();
});

// --- B11b final round (2026-11-03) ---

test('startRootSpan: throwing name/message/stack getters still give ERROR and an ended span', () => {
  const err = new Error('x');
  Object.defineProperty(err, 'name', { get() { throw new Error('name getter'); } });
  Object.defineProperty(err, 'message', { get() { throw new Error('message getter'); } });
  Object.defineProperty(err, 'stack', { get() { throw new Error('stack getter'); } });
  assert.throws(() => helper.startRootSpan('g1', () => { throw err; }), (e) => e === err);
  const s = byName('g1');
  assert.deepEqual(s.status, { code: api.SpanStatusCode.ERROR, message: 'Error: ' });
  const ev = s.events.find((e) => e.name === 'exception');
  assert.equal(ev.attributes['exception.type'], 'Error');
});

test('startRootSpan: a span whose setStatus and end throw does not replace the error', async () => {
  const err = new Error('orig');
  const calls = [];
  const span = {
    recordException() { calls.push('record'); },
    setStatus() { calls.push('status'); throw new Error('status'); },
    end() { calls.push('end'); throw new Error('end'); },
  };
  const tracer = { startActiveSpan: (_n, _o, _c, fn) => fn(span) };
  await assert.rejects(helper.startRootSpan('x', async () => { throw err; }, { tracer }), (e) => e === err);
  assert.deepEqual(calls, ['record', 'status', 'end']);
});
