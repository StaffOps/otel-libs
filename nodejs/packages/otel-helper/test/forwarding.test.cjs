'use strict';
// The stable forwarding providers and propagator (design D-JS10, task B9):
// objects obtained from them keep working when the delegate is swapped, and
// fall back to the API no-ops without one.
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('@opentelemetry/api');
const { W3CTraceContextPropagator } = require('@opentelemetry/core');
const { TracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const { MeterProvider, PeriodicExportingMetricReader, InMemoryMetricExporter, AggregationTemporality } = require('@opentelemetry/sdk-metrics');
const { LoggerProvider, InMemoryLogRecordExporter, SimpleLogRecordProcessor } = require('@opentelemetry/sdk-logs');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { createForwarders, NOOP_TRACER } = require('../dist/internal/forwarding.js');

function tracing() {
  const exporter = new InMemorySpanExporter();
  return { exporter, provider: new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter })] }) };
}

function metering() {
  const reader = new PeriodicExportingMetricReader({ exporter: new InMemoryMetricExporter(AggregationTemporality.CUMULATIVE), exportIntervalMillis: 3_600_000 });
  const provider = new MeterProvider({ readers: [reader] });
  /** name → summed point values (attributes ignored), from one collection. */
  const collect = async () => {
    const { resourceMetrics } = await reader.collect();
    const out = {};
    for (const scope of resourceMetrics.scopeMetrics) {
      for (const metric of scope.metrics) {
        out[metric.descriptor.name] = metric.dataPoints.map((p) => (typeof p.value === 'number' ? p.value : p.value.sum));
      }
    }
    return out;
  };
  /** `scope/name` → summed point values, from one collection. */
  const collectScoped = async () => {
    const { resourceMetrics } = await reader.collect();
    const out = {};
    for (const scope of resourceMetrics.scopeMetrics) {
      for (const metric of scope.metrics) out[`${scope.scope.name}/${metric.descriptor.name}`] = metric.dataPoints.map((p) => p.value);
    }
    return out;
  };
  return { provider, collect, collectScoped };
}

const remoteParent = () =>
  api.trace.setSpanContext(api.ROOT_CONTEXT, { traceId: '0af7651916cd43dd8448eb211c80319c', spanId: 'b7ad6b7169203331', traceFlags: 1, isRemote: true });

test('tracer: no-op without a delegate (parent span context kept), forwards to each new delegate', () => {
  const fw = createForwarders();
  const tracer = fw.tracerProvider.getTracer('t', '1.0.0', { schemaUrl: 'https://example.com/s' });
  const idle = tracer.startSpan('idle', {}, remoteParent());
  assert.equal(idle.isRecording(), false);
  assert.equal(idle.spanContext().spanId, 'b7ad6b7169203331');
  assert.equal(fw.tracerProvider.getDelegate(), undefined);
  const a = tracing();
  fw.tracerProvider.setDelegate(a.provider);
  assert.equal(fw.tracerProvider.getDelegate(), a.provider);
  tracer.startSpan('one').end();
  const b = tracing();
  fw.tracerProvider.setDelegate(b.provider);
  tracer.startSpan('two').end();
  assert.deepEqual(a.exporter.getFinishedSpans().map((s) => s.name), ['one']);
  assert.deepEqual(b.exporter.getFinishedSpans().map((s) => s.name), ['two']);
  assert.equal(b.exporter.getFinishedSpans()[0].instrumentationScope.version, '1.0.0');
  assert.equal(b.exporter.getFinishedSpans()[0].instrumentationScope.schemaUrl, 'https://example.com/s');
});

test('tracer: startActiveSpan forwards every overload and returns the callback result', () => {
  const fw = createForwarders();
  const { exporter, provider } = tracing();
  fw.tracerProvider.setDelegate(provider);
  const tracer = fw.tracerProvider.getTracer('t');
  const end = (value) => (span) => {
    span.end();
    return value;
  };
  assert.equal(tracer.startActiveSpan('two-args', end(1)), 1);
  assert.equal(tracer.startActiveSpan('three-args', { attributes: { k: 'v' } }, end(2)), 2);
  assert.equal(tracer.startActiveSpan('four-args', {}, remoteParent(), end(3)), 3);
  const spans = exporter.getFinishedSpans();
  assert.deepEqual(spans.map((s) => s.name), ['two-args', 'three-args', 'four-args']);
  assert.equal(spans[1].attributes.k, 'v');
  assert.equal(spans[2].spanContext().traceId, '0af7651916cd43dd8448eb211c80319c');
  fw.tracerProvider.setDelegate(undefined);
  assert.equal(tracer.startActiveSpan('idle', (span) => span.isRecording()), false);
});

test('meter: synchronous instruments are no-ops without a delegate and follow every swap', async () => {
  const fw = createForwarders();
  const meter = fw.meterProvider.getMeter('m', '1.0.0');
  const counter = meter.createCounter('c');
  const upDown = meter.createUpDownCounter('ud');
  const histogram = meter.createHistogram('h');
  const gauge = meter.createGauge('g');
  counter.add(100);
  const a = metering();
  fw.meterProvider.setDelegate(a.provider);
  assert.equal(fw.meterProvider.getDelegate(), a.provider);
  counter.add(1);
  upDown.add(-2);
  histogram.record(3);
  gauge.record(4);
  assert.deepEqual(await a.collect(), { c: [1], ud: [-2], h: [3], g: [4] });
  const b = metering();
  fw.meterProvider.setDelegate(b.provider);
  counter.add(5, { k: 'v' });
  assert.deepEqual(await b.collect(), { c: [5] });
  assert.deepEqual((await a.collect()).c, [1], 'nothing more reaches the old provider');
});

test('meter: observable callbacks are registered with every new delegate; duplicates and unknown removals are ignored', async () => {
  const fw = createForwarders();
  const meter = fw.meterProvider.getMeter('m');
  const gauge = meter.createObservableGauge('og');
  const counter = meter.createObservableCounter('oc');
  const upDown = meter.createObservableUpDownCounter('oud');
  const calls = { og: 0, oc: 0, oud: 0 };
  const cb = (result) => {
    calls.og++;
    result.observe(7);
  };
  const ocb = (result) => {
    calls.oc++;
    result.observe(8);
  };
  gauge.addCallback(cb);
  gauge.addCallback(cb);
  counter.addCallback(ocb);
  gauge.removeCallback(() => undefined); // never added: no-op
  const a = metering();
  fw.meterProvider.setDelegate(a.provider);
  // Added once the instrument exists on the current delegate.
  upDown.addCallback((result) => {
    calls.oud++;
    result.observe(-9);
  });
  assert.deepEqual(await a.collect(), { og: [7], oc: [8], oud: [-9] });
  assert.deepEqual(calls, { og: 1, oc: 1, oud: 1 });
  const b = metering();
  fw.meterProvider.setDelegate(b.provider);
  assert.deepEqual(await b.collect(), { og: [7], oc: [8], oud: [-9] });
  assert.deepEqual(calls, { og: 2, oc: 2, oud: 2 });
  // Cumulative readers keep reporting the last points: count the calls instead.
  gauge.removeCallback(cb);
  await b.collect();
  assert.deepEqual(calls, { og: 2, oc: 3, oud: 3 });
  // Removed while there is no delegate: not registered with the next one either.
  fw.meterProvider.setDelegate(undefined);
  counter.removeCallback(ocb);
  counter.removeCallback(cb); // a callback this instrument never had
  const c = metering();
  fw.meterProvider.setDelegate(c.provider);
  assert.deepEqual(await c.collect(), { oud: [-9] });
  assert.deepEqual(calls, { og: 2, oc: 3, oud: 4 });
});

test('meter: a delegate seen again (or set twice) calls each callback once', async () => {
  const fw = createForwarders();
  const meter = fw.meterProvider.getMeter('m');
  const gauge = meter.createObservableGauge('again');
  let calls = 0;
  gauge.addCallback((result) => {
    calls++;
    result.observe(1);
  });
  let batches = 0;
  meter.addBatchObservableCallback((result) => {
    batches++;
    result.observe(gauge, 2);
  }, [gauge]);
  const a = metering();
  const b = metering();
  fw.meterProvider.setDelegate(a.provider);
  fw.meterProvider.setDelegate(a.provider);
  fw.meterProvider.setDelegate(b.provider);
  fw.meterProvider.setDelegate(a.provider);
  await a.collect();
  assert.deepEqual([calls, batches], [1, 1]);
});

test('meter: batch callbacks observe through the delegate instruments, follow swaps, and are removed', async () => {
  const fw = createForwarders();
  const meter = fw.meterProvider.getMeter('m');
  const one = meter.createObservableGauge('b1');
  const two = meter.createObservableCounter('b2');
  let calls = 0;
  const batch = (result) => {
    calls++;
    result.observe(one, 11);
    result.observe(two, 12);
  };
  meter.addBatchObservableCallback(batch, [one, two]);
  meter.addBatchObservableCallback(batch, [two, one]); // the same set: ignored
  const a = metering();
  fw.meterProvider.setDelegate(a.provider);
  assert.deepEqual(await a.collect(), { b1: [11], b2: [12] });
  assert.equal(calls, 1);
  // An observable of the delegate meter itself passes through unchanged.
  const raw = a.provider.getMeter('m').createObservableGauge('raw');
  let rawCalls = 0;
  const rawBatch = (result) => {
    rawCalls++;
    result.observe(raw, 13);
  };
  meter.addBatchObservableCallback(rawBatch, [raw]);
  assert.deepEqual((await a.collect()).raw, [13]);
  meter.removeBatchObservableCallback(rawBatch, [raw]);
  await a.collect();
  assert.deepEqual([calls, rawCalls], [3, 1]);
  const b = metering();
  fw.meterProvider.setDelegate(b.provider);
  assert.deepEqual(await b.collect(), { b1: [11], b2: [12] });
  assert.equal(calls, 4);
  meter.removeBatchObservableCallback(batch, [one]); // a different set: not this registration
  await b.collect();
  assert.equal(calls, 5);
  meter.removeBatchObservableCallback(batch, [one, two]);
  meter.removeBatchObservableCallback(batch, [one, two]); // already removed
  await b.collect();
  assert.equal(calls, 5);
});

test('meter: a batch callback added without a delegate is registered with the first one', async () => {
  const fw = createForwarders();
  const meter = fw.meterProvider.getMeter('m');
  const gauge = meter.createObservableGauge('early');
  meter.addBatchObservableCallback((result) => result.observe(gauge, 21), [gauge]);
  const a = metering();
  fw.meterProvider.setDelegate(a.provider);
  assert.deepEqual(await a.collect(), { early: [21] });
});

test('logger: no-op without a delegate, forwards emit and enabled to each new delegate', () => {
  const fw = createForwarders();
  const logger = fw.loggerProvider.getLogger('l', '1.0.0');
  assert.equal(logger.enabled(), false);
  logger.emit({ body: 'dropped' });
  const exporter = new InMemoryLogRecordExporter();
  const provider = new LoggerProvider({ processors: [new SimpleLogRecordProcessor({ exporter })] });
  fw.loggerProvider.setDelegate(provider);
  assert.equal(fw.loggerProvider.getDelegate(), provider);
  assert.equal(logger.enabled({ severityNumber: api.SeverityNumber?.INFO }), true);
  logger.emit({ body: 'kept' });
  assert.deepEqual(exporter.getFinishedLogRecords().map((r) => r.body), ['kept']);
  assert.equal(exporter.getFinishedLogRecords()[0].instrumentationScope.version, '1.0.0');
});

test('propagator: injects and extracts nothing without a delegate; forwards to the delegate', () => {
  const fw = createForwarders();
  const ctx = api.trace.setSpanContext(api.ROOT_CONTEXT, { traceId: '0af7651916cd43dd8448eb211c80319c', spanId: 'b7ad6b7169203331', traceFlags: 1 });
  const carrier = {};
  fw.propagator.inject(ctx, carrier, api.defaultTextMapSetter);
  assert.deepEqual(carrier, {});
  assert.equal(fw.propagator.extract(api.ROOT_CONTEXT, { traceparent: 'x' }, api.defaultTextMapGetter), api.ROOT_CONTEXT);
  assert.deepEqual(fw.propagator.fields(), []);
  assert.equal(fw.propagator.getDelegate(), undefined);
  const w3c = new W3CTraceContextPropagator();
  fw.propagator.setDelegate(w3c);
  assert.equal(fw.propagator.getDelegate(), w3c);
  fw.propagator.inject(ctx, carrier, api.defaultTextMapSetter);
  assert.match(carrier.traceparent, /^00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01$/);
  const extracted = fw.propagator.extract(api.ROOT_CONTEXT, carrier, api.defaultTextMapGetter);
  assert.equal(api.trace.getSpanContext(extracted)?.spanId, 'b7ad6b7169203331');
  assert.deepEqual(fw.propagator.fields(), ['traceparent', 'tracestate']);
  fw.propagator.setDelegate(undefined);
  assert.equal(fw.propagator.getDelegate(), undefined);
});

// ── B9 fix round ─────────────────────────────────────────────────────────

const VALID = { traceId: '0af7651916cd43dd8448eb211c80319c', spanId: 'b7ad6b7169203331', traceFlags: 1 };

test('NOOP_TRACER (L1): keeps a valid parent, none for root or an invalid parent; startActiveSpan overloads run the callback with the span active', () => {
  const manager = new AsyncLocalStorageContextManager().enable();
  assert.equal(api.context.setGlobalContextManager(manager), true);
  try {
    const parent = api.trace.setSpanContext(api.ROOT_CONTEXT, VALID);
    assert.equal(NOOP_TRACER.startSpan('child', {}, parent).spanContext().spanId, VALID.spanId);
    assert.equal(NOOP_TRACER.startSpan('child', undefined, parent).isRecording(), false);
    assert.equal(NOOP_TRACER.startSpan('root', { root: true }, parent).spanContext().spanId, api.INVALID_SPAN_CONTEXT.spanId);
    const invalid = api.trace.setSpanContext(api.ROOT_CONTEXT, { ...VALID, traceId: '0'.repeat(32) });
    assert.equal(NOOP_TRACER.startSpan('bad', {}, invalid).spanContext().traceId, api.INVALID_SPAN_CONTEXT.traceId);
    // Without a context argument: the active context.
    assert.equal(api.context.with(parent, () => NOOP_TRACER.startSpan('active').spanContext().spanId), VALID.spanId);
    const active = (span) => api.trace.getActiveSpan() === span;
    assert.equal(NOOP_TRACER.startActiveSpan('two', active), true);
    assert.equal(NOOP_TRACER.startActiveSpan('three', { root: true }, active), true);
    assert.equal(NOOP_TRACER.startActiveSpan('four', {}, parent, (span) => span.spanContext().spanId), VALID.spanId);
    assert.equal(api.context.with(parent, () => NOOP_TRACER.startActiveSpan('four-undef', {}, undefined, (span) => span.spanContext().spanId)), VALID.spanId);
    assert.equal(NOOP_TRACER.startActiveSpan('no-fn', {}), undefined);
  } finally {
    api.context.disable();
  }
});

test('M7 tracers, meters and loggers are cached by (name, version, schema URL)', () => {
  const fw = createForwarders();
  const t = fw.tracerProvider.getTracer('a', '1', { schemaUrl: 's' });
  assert.equal(fw.tracerProvider.getTracer('a', '1', { schemaUrl: 's' }), t);
  assert.notEqual(fw.tracerProvider.getTracer('a', '2', { schemaUrl: 's' }), t);
  assert.notEqual(fw.tracerProvider.getTracer('a', '1'), t);
  const m = fw.meterProvider.getMeter('a', '1');
  assert.equal(fw.meterProvider.getMeter('a', '1'), m);
  assert.notEqual(fw.meterProvider.getMeter('a'), m);
  const l = fw.loggerProvider.getLogger('a');
  assert.equal(fw.loggerProvider.getLogger('a'), l);
  assert.notEqual(fw.loggerProvider.getLogger('a', undefined, { schemaUrl: 's' }), l);
});

test('M7 retention: only observables with callbacks and meters with batches are tracked, and dropped once empty', () => {
  const fw = createForwarders();
  // The shape that retained everything before: a meter and an observable per call, no callback.
  for (let i = 0; i < 10_000; i++) fw.meterProvider.getMeter('hot').createObservableGauge(`g${i % 10}`);
  assert.equal(fw.meterProvider.watchedCount(), 0);
  const meter = fw.meterProvider.getMeter('hot');
  const gauge = meter.createObservableGauge('watched');
  const cb = (r) => r.observe(1);
  gauge.addCallback(cb);
  assert.equal(fw.meterProvider.watchedCount(), 1);
  gauge.removeCallback(cb);
  assert.equal(fw.meterProvider.watchedCount(), 0);
  const batch = (r) => r.observe(gauge, 1);
  meter.addBatchObservableCallback(batch, [gauge]);
  assert.equal(fw.meterProvider.watchedCount(), 1);
  meter.removeBatchObservableCallback(batch, [gauge]);
  assert.equal(fw.meterProvider.watchedCount(), 0);
});

test('H1 a delegate set to undefined leaves the callbacks on the last provider, for its final collection', async () => {
  const fw = createForwarders();
  const meter = fw.meterProvider.getMeter('m');
  const gauge = meter.createObservableGauge('final_g');
  const batched = meter.createObservableGauge('final_b');
  let calls = 0;
  gauge.addCallback((r) => {
    calls++;
    r.observe(1);
  });
  meter.addBatchObservableCallback((r) => {
    calls++;
    r.observe(batched, 2);
  }, [batched]);
  const a = metering();
  fw.meterProvider.setDelegate(a.provider);
  fw.meterProvider.setDelegate(undefined);
  // Added and removed while there is no delegate: kept for the next one, nothing else.
  const late = (r) => r.observe(3);
  gauge.addCallback(late);
  assert.deepEqual(await a.collect(), { final_g: [1], final_b: [2] });
  assert.equal(calls, 2);
  const b = metering();
  fw.meterProvider.setDelegate(b.provider);
  assert.deepEqual((await b.collect()).final_b, [2]);
  assert.equal(calls, 4);
});

test('M6 a batch re-homes only its own meter\'s observables; another meter\'s pass through unchanged', async () => {
  const fw = createForwarders();
  const m1 = fw.meterProvider.getMeter('m1');
  const m2 = fw.meterProvider.getMeter('m2');
  const own = m1.createObservableGauge('own');
  const other = m2.createObservableGauge('other');
  let otherCalls = 0;
  other.addCallback((r) => {
    otherCalls++;
    r.observe(1);
  });
  m1.addBatchObservableCallback((r) => {
    r.observe(own, 10);
    r.observe(other, 99); // not m1's: the SDK ignores it
  }, [own, other]);
  const a = metering();
  fw.meterProvider.setDelegate(a.provider);
  assert.deepEqual(await a.collectScoped(), { 'm1/own': [10], 'm2/other': [1] });
  assert.equal(otherCalls, 1);
});

test('M1 forceFlush forwards to the current delegate and resolves without one (or without the method)', async () => {
  const fw = createForwarders();
  for (const p of [fw.tracerProvider, fw.meterProvider, fw.loggerProvider]) {
    await p.forceFlush();
    let flushed = 0;
    p.setDelegate({ getTracer() {}, getMeter() {}, getLogger() {}, forceFlush: async () => { flushed++; } });
    await p.forceFlush();
    assert.equal(flushed, 1);
    p.setDelegate({ getTracer() {}, getMeter() {}, getLogger() {} });
    await p.forceFlush();
    p.setDelegate(undefined);
  }
});

test('final round: forceFlush turns a synchronous throw of the delegate into a rejection', async () => {
  const fw = createForwarders();
  fw.tracerProvider.setDelegate({ getTracer() {}, forceFlush() { throw new Error('sync flush'); } });
  const pending = fw.tracerProvider.forceFlush();
  assert.ok(pending instanceof Promise);
  await assert.rejects(pending, /sync flush/);
});
