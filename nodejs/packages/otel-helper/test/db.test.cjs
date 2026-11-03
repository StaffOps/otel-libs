'use strict';
// The internal entry @staffops/otel-helper/internal/db (task B10b, 2026-11-03;
// moved from @staffops/otel-helper-sql, B10a): the database-span rules the
// opt-in -sql and -redis packages share — the OTEL_SEMCONV_STABILITY_OPT_IN
// database grammar (Go `ext/otelsql` legacyKeysEnabled, contract A25), the
// legacy db.system copy, the tracer wrapper (ERROR description dropped with
// error.type _OTHER, span-name rule) and the meter wrapper (legacy key,
// generation guard). The packages' suites run them against real servers.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const api = require('@opentelemetry/api');
const { InMemorySpanExporter, SimpleSpanProcessor, TracerProvider } = require('@opentelemetry/sdk-trace');
const { MeterProvider, MetricReader } = require('@opentelemetry/sdk-metrics');
const db = require('@staffops/otel-helper/internal/db');

test('internal/db exports exactly what the opt-in DB packages use', () => {
  assert.deepEqual(Object.keys(db).filter((k) => k !== '__esModule').sort(), [
    'ATTR_DB_SYSTEM',
    'ATTR_DB_SYSTEM_NAME',
    'ENV_SEMCONV_OPT_IN',
    'LEGACY_DB_SYSTEM',
    'dbMeterProvider',
    'dbTracerProvider',
    'legacyKeysEnabled',
    'legacyKeysMode',
    'withLegacyKey',
  ]);
  assert.deepEqual({ ...db.LEGACY_DB_SYSTEM }, { postgresql: 'postgresql', mysql: 'mysql', redis: 'redis' });
  assert.ok(Object.isFrozen(db.LEGACY_DB_SYSTEM));
});

test('legacy db.system unless the value holds database without database/dup (A25)', () => {
  const table = [
    [undefined, true],
    ['', true],
    ['http', true],
    ['http,database/dup', true],
    ['database', false],
    ['DATABASE', false],
    [' Database ', false],
    ['http,DATABASE', false],
    ['database,database/dup', true],
    ['\tdatabase/DUP\n', true],
    ['databasé', true], // not a token: ASCII folding only (contract D1)
    ['database ', true], // NBSP is not in the trim set
  ];
  for (const [value, want] of table) assert.equal(db.legacyKeysEnabled(value), want, JSON.stringify(value));
  const saved = process.env.OTEL_SEMCONV_STABILITY_OPT_IN;
  process.env.OTEL_SEMCONV_STABILITY_OPT_IN = 'database';
  try {
    assert.equal(db.legacyKeysEnabled(), false, 'reads the variable by default');
  } finally {
    if (saved === undefined) delete process.env.OTEL_SEMCONV_STABILITY_OPT_IN;
    else process.env.OTEL_SEMCONV_STABILITY_OPT_IN = saved;
  }
});

test('legacyKeysMode reads the variable once per process (the first DB factory call)', () => {
  const entry = require.resolve('@staffops/otel-helper/internal/db');
  const res = spawnSync(
    process.execPath,
    ['-e', `const db = require(${JSON.stringify(entry)}); const first = db.legacyKeysMode(); delete process.env.OTEL_SEMCONV_STABILITY_OPT_IN; console.log(JSON.stringify([first, db.legacyKeysMode()]));`],
    { encoding: 'utf8', env: { ...process.env, OTEL_SEMCONV_STABILITY_OPT_IN: 'database' }, cwd: path.resolve(__dirname, '..') },
  );
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(JSON.parse(res.stdout), [false, false]);
});

test('withLegacyKey copies db.system.name into db.system (mapped), never over an existing db.system', () => {
  assert.equal(db.withLegacyKey(undefined), undefined);
  const plain = { a: 1 };
  assert.equal(db.withLegacyKey(plain), plain, 'no db.system.name: unchanged');
  assert.deepEqual(db.withLegacyKey({ 'db.system.name': 'redis' }), { 'db.system.name': 'redis', 'db.system': 'redis' });
  assert.deepEqual(db.withLegacyKey({ 'db.system.name': 'other_sql' }), { 'db.system.name': 'other_sql', 'db.system': 'other_sql' });
  const own = { 'db.system.name': 'postgresql', 'db.system': 'custom' };
  assert.equal(db.withLegacyKey(own), own);
  assert.equal(db.withLegacyKey({ 'db.system.name': 5 })['db.system'], undefined, 'only string values');
});

function tracerOf(opts) {
  const exporter = new InMemorySpanExporter();
  const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  return { exporter, provider, wrapped: db.dbTracerProvider(provider, opts) };
}

test('the DB tracer adds db.system at span start (startSpan and every startActiveSpan form)', () => {
  const { exporter, wrapped } = tracerOf({ legacyKeys: true });
  const tracer = wrapped.getTracer('t', '1');
  const attrs = { 'db.system.name': 'redis' };
  tracer.startSpan('a', { attributes: attrs }).end();
  tracer.startSpan('b').end();
  tracer.startSpan('c', { kind: api.SpanKind.CLIENT }).end();
  tracer.startSpan('d', { attributes: { other: 'x' } }).end();
  assert.equal(tracer.startActiveSpan('e', (span) => (span.end(), 'r1')), 'r1');
  assert.equal(tracer.startActiveSpan('f', { attributes: attrs }, (span) => (span.end(), 'r2')), 'r2');
  assert.equal(tracer.startActiveSpan('g', { attributes: attrs }, api.context.active(), (span) => (span.end(), 'r3')), 'r3');
  const byName = Object.fromEntries(exporter.getFinishedSpans().map((s) => [s.name, s.attributes]));
  for (const name of ['a', 'f', 'g']) assert.equal(byName[name]['db.system'], 'redis', name);
  for (const name of ['b', 'c', 'd', 'e']) assert.equal(byName[name]['db.system'], undefined, name);
  assert.deepEqual(attrs, { 'db.system.name': 'redis' }, 'the caller attributes are not mutated');
  const stable = tracerOf({ legacyKeys: false });
  stable.wrapped.getTracer('t').startSpan('s', { attributes: attrs }).end();
  assert.equal(stable.exporter.getFinishedSpans()[0].attributes['db.system'], undefined);
});

test('the optional span-name rule gets the requested name and the start attributes', () => {
  const seen = [];
  const { exporter, wrapped } = tracerOf({
    legacyKeys: false,
    spanName: (requested, attributes) => (seen.push([requested, attributes?.k]), `renamed-${requested}`),
  });
  const tracer = wrapped.getTracer('t');
  tracer.startSpan('one', { attributes: { k: 'v' } }).end();
  tracer.startActiveSpan('two', (s) => s.end());
  assert.deepEqual(exporter.getFinishedSpans().map((s) => s.name), ['renamed-one', 'renamed-two']);
  assert.deepEqual(seen, [['one', 'v'], ['two', undefined]]);
});

test('A25: an ERROR status keeps its code and drops the description; error.type _OTHER unless set; other statuses unchanged', () => {
  const { exporter, wrapped } = tracerOf({ legacyKeys: false });
  const tracer = wrapped.getTracer('t');
  const failed = tracer.startSpan('failed');
  assert.equal(failed.setStatus({ code: api.SpanStatusCode.ERROR, message: "ERR unknown command 'X', with args beginning with: 'secret'" }), failed, 'chainable');
  failed.recordException(new Error('kept'));
  failed.end();
  const ok = tracer.startSpan('ok');
  ok.setStatus({ code: api.SpanStatusCode.OK, message: 'fine' });
  ok.end();
  tracer.startActiveSpan('active', (span) => {
    span.setStatus({ code: api.SpanStatusCode.ERROR, message: 'secret' });
    span.end();
  });
  const typed = tracer.startSpan('typed', { attributes: { 'error.type': 'WRONGTYPE' } });
  typed.setStatus({ code: api.SpanStatusCode.ERROR, message: 'x' });
  typed.end();
  const byName = Object.fromEntries(exporter.getFinishedSpans().map((s) => [s.name, s]));
  assert.deepEqual(byName.failed.status, { code: api.SpanStatusCode.ERROR });
  assert.equal(byName.failed.attributes['error.type'], '_OTHER');
  assert.equal(byName.failed.events[0].attributes['exception.message'], 'kept', 'exception events are kept (A25 residual)');
  assert.deepEqual(byName.active.status, { code: api.SpanStatusCode.ERROR });
  assert.equal(byName.ok.status.code, api.SpanStatusCode.OK);
  assert.equal(byName.ok.attributes['error.type'], undefined);
  assert.equal(byName.typed.attributes['error.type'], 'WRONGTYPE', 'an existing error.type is kept');
  const frozen = Object.freeze({ setStatus() {} });
  const tp = db.dbTracerProvider({ getTracer: () => ({ startSpan: () => frozen }) }, { legacyKeys: false });
  assert.equal(tp.getTracer('x').startSpan('y'), frozen, 'a frozen span is left as it is');
});

test('A26: responseStatusCode sets db.response.status_code and error.type from the dropped description; none -> _OTHER', () => {
  const { exporter, wrapped } = tracerOf({ legacyKeys: false, responseStatusCode: (d) => /^([A-Z]+) /.exec(d)?.[1] });
  const tracer = wrapped.getTracer('t');
  const coded = tracer.startSpan('coded');
  coded.setStatus({ code: api.SpanStatusCode.ERROR, message: 'WRONGTYPE Operation against a key' });
  coded.end();
  const plain = tracer.startSpan('plain');
  plain.setStatus({ code: api.SpanStatusCode.ERROR, message: 'connect ECONNREFUSED 127.0.0.1:1' });
  plain.end();
  const empty = tracer.startSpan('empty');
  empty.setStatus({ code: api.SpanStatusCode.ERROR });
  empty.end();
  const byName = Object.fromEntries(exporter.getFinishedSpans().map((s) => [s.name, s]));
  assert.deepEqual(byName.coded.status, { code: api.SpanStatusCode.ERROR });
  assert.equal(byName.coded.attributes['db.response.status_code'], 'WRONGTYPE');
  assert.equal(byName.coded.attributes['error.type'], 'WRONGTYPE');
  assert.equal(byName.plain.attributes['error.type'], '_OTHER');
  assert.equal(byName.plain.attributes['db.response.status_code'], undefined);
  assert.equal(byName.empty.attributes['error.type'], '_OTHER');
});

test('wrappers forward the members they do not change (forceFlush, getDelegate, unknown ones)', async () => {
  let flushed = 0;
  const inner = { getTracer: () => ({ startSpan: () => api.trace.wrapSpanContext(api.INVALID_SPAN_CONTEXT), extra: () => 'tracer-extra' }), forceFlush: () => ((flushed += 1), Promise.resolve()), getDelegate: () => 'delegate' };
  const tp = db.dbTracerProvider(inner, { legacyKeys: false });
  await tp.forceFlush();
  assert.equal(flushed, 1);
  assert.equal(tp.getDelegate(), 'delegate');
  assert.equal(tp.getTracer('x').extra(), 'tracer-extra');
  const mp = db.dbMeterProvider({ getMeter: () => ({ unknownMember: 42 }), forceFlush: () => 'mp-flush' }, { legacyKeys: true, current: () => true });
  assert.equal(mp.forceFlush(), 'mp-flush');
  assert.equal(mp.getMeter('m').unknownMember, 42);
});

class TestReader extends MetricReader {
  onForceFlush() {
    return Promise.resolve();
  }
  onShutdown() {
    return Promise.resolve();
  }
}

test('the DB meter adds db.system to synchronous measurements; observables pass through', async () => {
  const reader = new TestReader();
  const provider = new MeterProvider({ readers: [reader] });
  const meter = db.dbMeterProvider(provider, { legacyKeys: true, current: () => true }).getMeter('m', '1');
  const attrs = { 'db.system.name': 'mysql' };
  meter.createCounter('c').add(1, attrs);
  meter.createUpDownCounter('u').add(2, attrs);
  meter.createHistogram('h').record(0.5, attrs);
  meter.createGauge('g').record(3, attrs);
  meter.createCounter('plain').add(1);
  meter.createObservableGauge('og').addCallback((r) => r.observe(1, attrs));
  const { resourceMetrics } = await reader.collect();
  const points = Object.fromEntries(resourceMetrics.scopeMetrics[0].metrics.map((m) => [m.descriptor.name, m.dataPoints.map((p) => p.attributes)]));
  for (const name of ['c', 'u', 'h', 'g']) assert.deepEqual(points[name], [{ 'db.system.name': 'mysql', 'db.system': 'mysql' }], name);
  assert.deepEqual(points.plain, [{}]);
  assert.deepEqual(points.og, [{ 'db.system.name': 'mysql' }], 'observable measurements are not rewritten');
  const fake = db.dbMeterProvider({ getMeter: () => ({ createCounter: () => ({ other: 1 }) }) }, { legacyKeys: true, current: () => true });
  assert.deepEqual(fake.getMeter('m').createCounter('x').other, 1, 'an instrument without add is returned as it is');
  await provider.shutdown();
});

test('instruments of an older meter bind record nothing; without legacy keys attributes pass unchanged', async () => {
  const reader = new TestReader();
  const provider = new MeterProvider({ readers: [reader] });
  let generation = 1;
  const old = db.dbMeterProvider(provider, { legacyKeys: false, current: () => generation === 1 }).getMeter('m');
  const counter = old.createUpDownCounter('max');
  counter.add(3, { 'db.system.name': 'postgresql' });
  generation = 2;
  counter.add(-3, { 'db.system.name': 'postgresql' });
  const { resourceMetrics } = await reader.collect();
  const point = resourceMetrics.scopeMetrics[0].metrics[0].dataPoints[0];
  assert.equal(point.value, 3, 'the stale subtraction was dropped');
  assert.deepEqual(point.attributes, { 'db.system.name': 'postgresql' });
  await provider.shutdown();
});
