'use strict';
// @staffops/otel-helper-sql without a database (task B10a and its fix round of
// 2026-11-03; spec JS8, design D-JS9, contract R10, D14 and A25): options, the
// library detection and load errors, the OTEL_SEMCONV_STABILITY_OPT_IN grammar
// (Go `ext/otelsql` legacyKeysEnabled), the tracer and meter wrappers (legacy
// key, ERROR description dropped, mysql2 span names, meter generations), the
// preloaded-library re-require on a stub `pg`, and the lifecycle through
// setup(). db.test.cjs runs the instrumentation against PostgreSQL and MySQL.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const api = require('@opentelemetry/api');
const { InMemorySpanExporter, SimpleSpanProcessor, TracerProvider } = require('@opentelemetry/sdk-trace');
const { MeterProvider, MetricReader } = require('@opentelemetry/sdk-metrics');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { sqlInstrumentation } = require('@staffops/otel-helper-sql');
const semconv = require('../dist/internal/semconv.js');
const providers = require('../dist/internal/providers.js');
const libraries = require('../dist/internal/libraries.js');
const { childrenForTests } = require('../dist/internal/sql-instrumentation.js');

const ENTRY = require.resolve('@staffops/otel-helper-sql');
const SQL_DIR = path.resolve(__dirname, '..');

let saved;
let records;
test.beforeEach(() => {
  saved = { ...process.env };
  for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  testing.reset();
  records = [];
  testing.setDiagnosticSink((line) => records.push(line));
});
test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

/** Runs `source` (CommonJS) as `<dir>/main.cjs` with cwd `dir`; returns the JSON it prints last and its stderr. */
function runIn(dir, source, env = {}) {
  const script = path.join(dir, 'main.cjs');
  fs.writeFileSync(script, source);
  const res = spawnSync(process.execPath, [script], {
    cwd: dir,
    encoding: 'utf8',
    timeout: 60_000,
    env: { ...process.env, OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=DEV', OTEL_HELPER_METRICS_PORT: '0', ...env },
  });
  const last = res.stdout.trim().split('\n').pop();
  try {
    return { ...JSON.parse(last), stderr: res.stderr };
  } catch {
    throw new Error(`child status=${res.status}\nstdout:\n${res.stdout}\nstderr:\n${res.stderr}`);
  }
}

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'otel-sql-'));
}

/** A stub `pg` 8.x (main entry + lib/client.js) under `<dir>/node_modules/pg`, enough for instrumentation-pg to patch. */
function stubPg(dir) {
  const root = path.join(dir, 'node_modules', 'pg');
  fs.mkdirSync(path.join(root, 'lib'), { recursive: true });
  fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ name: 'pg', version: '8.20.0', main: 'index.js' }));
  fs.writeFileSync(path.join(root, 'lib', 'client.js'), 'module.exports = class Client { query() { return "q"; } connect() {} };\n');
  fs.writeFileSync(path.join(root, 'index.js'), "module.exports = { Client: require('./lib/client.js') };\n");
  return root;
}

// ── options ────────────────────────────────────────────────────────────

test('invalid options throw a TypeError naming the problem (R8)', () => {
  class Opts {}
  for (const [value, message] of [
    [null, /options must be an object, got null/],
    [[], /options must be an object, got array/],
    ['pg', /options must be an object, got string/],
    [new Opts(), /options must be an object, got object/],
    [{ pg: true, redis: true }, /unknown key\(s\) redis/],
    [{ pg: 'yes' }, /pg must be a boolean, got string/],
    [{ mysql2: 1 }, /mysql2 must be a boolean, got number/],
    [{ pg: false, mysql2: false }, /both false; nothing to instrument/],
  ]) {
    assert.throws(() => sqlInstrumentation(value), { name: 'TypeError', message }, JSON.stringify(value));
  }
});

test('undefined option values count as not set; a null-prototype object is accepted', () => {
  const opts = Object.create(null);
  opts.pg = undefined;
  opts.other = undefined;
  assert.deepEqual([...sqlInstrumentation(opts).libraries], ['pg', 'mysql2']);
  assert.deepEqual([...sqlInstrumentation({ mysql2: false }).libraries], ['pg']);
  assert.deepEqual([...sqlInstrumentation({ pg: false, mysql2: true }).libraries], ['mysql2']);
});

test('the result is one Instrumentation named after the package, covering the contrib names (M2)', () => {
  const inst = sqlInstrumentation();
  assert.equal(inst.instrumentationName, '@staffops/otel-helper-sql');
  assert.equal(inst.instrumentationVersion, require('../package.json').version);
  assert.deepEqual([...inst.coveredInstrumentations], ['@opentelemetry/instrumentation-pg', '@opentelemetry/instrumentation-mysql2']);
  assert.deepEqual([...sqlInstrumentation({ pg: false }).coveredInstrumentations], ['@opentelemetry/instrumentation-mysql2']);
  assert.deepEqual(inst.getConfig(), { enabled: false });
  inst.setConfig({ enabled: true, other: 1 });
  assert.deepEqual(inst.getConfig(), { enabled: false }, 'setConfig is ignored: enabled is the real state (L3)');
});

test('M2: a raw contrib instrumentation next to sqlInstrumentation() is refused by setup()', () => {
  const { MySQL2Instrumentation } = require('@opentelemetry/instrumentation-mysql2');
  const raw = new MySQL2Instrumentation({ enabled: false });
  assert.throws(() => helper.setup({ instrumentations: [sqlInstrumentation(), raw] }), {
    name: 'TypeError',
    message: /two instrumentations for "@opentelemetry\/instrumentation-mysql2"/,
  });
  assert.throws(() => helper.setup({ instrumentations: [sqlInstrumentation(), sqlInstrumentation({ mysql2: false })] }), {
    message: /two instrumentations for "@staffops\/otel-helper-sql"/,
  });
});

// ── library detection and D14 load errors (R3) ────────────────────────

test('R3: unresolved libraries are still instrumented; WARN only when named true, diag debug in default mode (2026-11-03)', () => {
  const dir = tempDir();
  try {
    const src = (opts) => `
      const helper = require(${JSON.stringify(require.resolve('@staffops/otel-helper'))});
      const inst = require(${JSON.stringify(ENTRY)}).sqlInstrumentation(${opts});
      helper.setup({ instrumentations: [inst] });
      console.log(JSON.stringify({ libraries: inst.libraries }));`;
    const lines = (out, lib) => out.stderr.split('\n').filter((l) => l.includes(`'${lib}' could not be resolved from`));
    const quiet = runIn(dir, src(''));
    assert.deepEqual(quiet.libraries, ['pg', 'mysql2']);
    assert.deepEqual([...lines(quiet, 'pg'), ...lines(quiet, 'mysql2')], [], 'default mode: no WARN for an absent library');
    const debug = runIn(dir, src(''), { OTEL_LOG_LEVEL: 'DEBUG' });
    for (const lib of ['pg', 'mysql2']) assert.equal(lines(debug, lib).length, 1, `debug line for ${lib}: ${debug.stderr}`);
    const named = runIn(dir, src('{ pg: true, mysql2: true }'));
    assert.deepEqual(named.libraries, ['pg', 'mysql2'], 'named true: never refused because of resolution');
    for (const lib of ['pg', 'mysql2']) {
      const got = lines(named, lib);
      assert.equal(got.length, 1, named.stderr);
      assert.ok(got[0].includes(dir) && got[0].includes('; instrumented if it loads'), got[0]);
      assert.ok(!got[0].includes('node_modules'), `the WARN names the entry directory and cwd only (L-d): ${got[0]}`);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/** Child source that makes the contrib modules in `poisoned` throw on load (a require.cache entry with a throwing getter). */
function poison(poisoned) {
  return poisoned
    .map((mod) => `{ const f = require.resolve(${JSON.stringify(mod)}, { paths: [${JSON.stringify(SQL_DIR)}] });
      const m = new (require('node:module'))(f); m.loaded = true; m.filename = f;
      Object.defineProperty(m, 'exports', { get() { throw new Error('contrib boom'); } });
      require.cache[f] = m; }`)
    .join('\n');
}

test('D14: no contrib instrumentation loads -> the first load error is thrown; named true -> its own', () => {
  const dir = tempDir();
  try {
    const both = runIn(dir, `${poison(['@opentelemetry/instrumentation-pg', '@opentelemetry/instrumentation-mysql2'])}
      try { require(${JSON.stringify(ENTRY)}).sqlInstrumentation(); console.log('{}'); }
      catch (e) { console.log(JSON.stringify({ message: e.message, cause: e.cause && e.cause.message })); }`);
    assert.deepEqual({ message: both.message, cause: both.cause }, { message: 'failed to import the SQL instrumentation: contrib boom', cause: 'contrib boom' });
    const named = runIn(dir, `${poison(['@opentelemetry/instrumentation-pg'])}
      try { require(${JSON.stringify(ENTRY)}).sqlInstrumentation({ pg: true }); console.log('{}'); }
      catch (e) { console.log(JSON.stringify({ message: e.message })); }`);
    assert.equal(named.message, 'failed to import the SQL instrumentation: contrib boom');
    const one = runIn(dir, `${poison(['@opentelemetry/instrumentation-pg'])}
      const helper = require(${JSON.stringify(require.resolve('@staffops/otel-helper'))});
      const inst = require(${JSON.stringify(ENTRY)}).sqlInstrumentation();
      helper.setup({ instrumentations: [inst] });
      console.log(JSON.stringify({ libraries: inst.libraries }));`);
    assert.deepEqual(one.libraries, ['mysql2']);
    assert.match(one.stderr, /'pg' is not instrumented: failed to import the SQL instrumentation: contrib boom/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('the libraries are resolved from the app without being loaded', () => {
  const loaded = () => Object.keys(require.cache).filter((f) => /[\\/]node_modules[\\/](pg|mysql2)[\\/]/.test(f));
  assert.deepEqual(loaded(), [], 'this test file never loads pg or mysql2');
  sqlInstrumentation();
  assert.deepEqual(loaded(), []);
});

test('appDirectories: entry script directory, working directory, main lookup paths (deduplicated)', () => {
  assert.deepEqual(libraries.appDirectories('/srv/app/main.js', '/work', []), ['/srv/app', '/work']);
  assert.deepEqual(libraries.appDirectories('main.js', '/work', ['/work/node_modules', '/node_modules']), ['/work', '/work/node_modules', '/node_modules']);
  assert.deepEqual(libraries.appDirectories('', '/work', []), ['/work']);
  assert.ok(libraries.appDirectories().length >= 1, 'defaults read the process');
  assert.equal(libraries.resolvesFromApp('pg', [__dirname]), true);
  assert.equal(libraries.resolvesFromApp('no-such-package-otel-helper', [__dirname]), false);
});

test('D14: a contrib module that is missing is "not installed"; any other load error is reported verbatim', () => {
  const missing = Object.assign(new Error("Cannot find module '@opentelemetry/instrumentation-pg'\nRequire stack:\n- /x/index.js"), { code: 'MODULE_NOT_FOUND' });
  const notInstalled = libraries.instrumentationLoadError('@opentelemetry/instrumentation-pg', missing);
  assert.equal(
    notInstalled.message,
    'SQL instrumentation not installed: @opentelemetry/instrumentation-pg is missing. Reinstall @staffops/otel-helper-sql (it depends on @opentelemetry/instrumentation-pg).',
  );
  assert.equal(notInstalled.cause, missing);
  const transitive = Object.assign(new Error("Cannot find module '@opentelemetry/sql-common'\nRequire stack:\n- /x"), { code: 'MODULE_NOT_FOUND' });
  assert.equal(
    libraries.instrumentationLoadError('@opentelemetry/instrumentation-pg', transitive).message,
    "failed to import the SQL instrumentation: Cannot find module '@opentelemetry/sql-common'",
  );
  assert.equal(libraries.instrumentationLoadError('m', new TypeError('boom')).message, 'failed to import the SQL instrumentation: boom');
  assert.equal(libraries.instrumentationLoadError('m', 'thrown string').message, 'failed to import the SQL instrumentation: thrown string');
});

// ── preloaded and nested libraries on a stub pg (R3, R4) ──────────────

const SETUP_SRC = `
  const helper = require(${JSON.stringify(require.resolve('@staffops/otel-helper'))});
  const { sqlInstrumentation } = require(${JSON.stringify(ENTRY)});`;

test('R4: a pg loaded before sqlInstrumentation() is patched on the first enable (main entry and lib/client.js re-required)', () => {
  const dir = tempDir();
  try {
    stubPg(dir);
    const out = runIn(dir, `
      const pg = require('pg');
      const before = new pg.Client();
      ${SETUP_SRC}
      helper.setup({ instrumentations: [sqlInstrumentation({ pg: true })] });
      const wrapped = pg.Client.prototype.query.__wrapped === true;
      helper.shutdown().then(() => console.log(JSON.stringify({ wrapped, sameProto: Object.getPrototypeOf(before) === pg.Client.prototype,
        unwrapped: pg.Client.prototype.query.__wrapped === undefined })));`);
    assert.deepEqual({ wrapped: out.wrapped, sameProto: out.sameProto, unwrapped: out.unwrapped }, { wrapped: true, sameProto: true, unwrapped: true });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('R3: a nested, non-hoisted pg named true that the app dirs do not resolve is warned about and instrumented when loaded', () => {
  const dir = tempDir();
  try {
    const app = path.join(dir, 'app');
    stubPg(app);
    fs.writeFileSync(path.join(app, 'db.cjs'), "module.exports = require('pg');\n");
    const out = runIn(dir, `${SETUP_SRC}
      const inst = sqlInstrumentation({ pg: true, mysql2: false });
      helper.setup({ instrumentations: [inst] });
      const pg = require('./app/db.cjs');
      console.log(JSON.stringify({ libraries: inst.libraries, wrapped: pg.Client.prototype.query.__wrapped === true }));`);
    assert.deepEqual(out.libraries, ['pg']);
    assert.equal(out.wrapped, true);
    assert.match(out.stderr, /'pg' could not be resolved from .*; instrumented if it loads/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ── OTEL_SEMCONV_STABILITY_OPT_IN (design D-JS9; Go legacyKeysEnabled) ─

test('legacy db.system unless the value holds database without database/dup', () => {
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
  for (const [value, want] of table) assert.equal(semconv.legacyKeysEnabled(value), want, JSON.stringify(value));
  process.env.OTEL_SEMCONV_STABILITY_OPT_IN = 'database';
  assert.equal(semconv.legacyKeysEnabled(), false, 'reads the variable by default');
});

test('the mode is read once per process, by the first sqlInstrumentation() (L5)', () => {
  const dir = tempDir();
  try {
    const out = runIn(dir, `
      const { legacyKeysMode } = require(${JSON.stringify(path.join(SQL_DIR, 'dist/internal/sql-instrumentation.js'))});
      require(${JSON.stringify(ENTRY)}).sqlInstrumentation();
      const first = legacyKeysMode();
      delete process.env.OTEL_SEMCONV_STABILITY_OPT_IN;
      require(${JSON.stringify(ENTRY)}).sqlInstrumentation();
      console.log(JSON.stringify({ first, second: legacyKeysMode() }));`, { OTEL_SEMCONV_STABILITY_OPT_IN: 'database' });
    assert.equal(out.first, false);
    assert.equal(out.second, false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('withLegacyKey copies db.system.name into db.system (mapped), never over an existing db.system', () => {
  assert.equal(semconv.withLegacyKey(undefined), undefined);
  const plain = { a: 1 };
  assert.equal(semconv.withLegacyKey(plain), plain, 'no db.system.name: unchanged');
  assert.deepEqual(semconv.withLegacyKey({ 'db.system.name': 'postgresql' }), { 'db.system.name': 'postgresql', 'db.system': 'postgresql' });
  assert.deepEqual(semconv.withLegacyKey({ 'db.system.name': 'other_sql' }), { 'db.system.name': 'other_sql', 'db.system': 'other_sql' });
  const own = { 'db.system.name': 'postgresql', 'db.system': 'custom' };
  assert.equal(semconv.withLegacyKey(own), own);
  assert.equal(semconv.withLegacyKey({ 'db.system.name': 5 })['db.system'], undefined, 'only string values');
});

// ── wrappers ──────────────────────────────────────────────────────────

function tracerOf(opts) {
  const exporter = new InMemorySpanExporter();
  const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  return { exporter, provider, wrapped: providers.dbTracerProvider(provider, opts) };
}

test('the DB tracer adds db.system at span start (startSpan and every startActiveSpan form)', () => {
  const { exporter, wrapped } = tracerOf({ legacyKeys: true, normalizeSpanName: false });
  const tracer = wrapped.getTracer('t', '1');
  const attrs = { 'db.system.name': 'postgresql' };
  tracer.startSpan('a', { attributes: attrs }).end();
  tracer.startSpan('b').end();
  tracer.startSpan('c', { kind: api.SpanKind.CLIENT }).end();
  tracer.startSpan('d', { attributes: { other: 'x' } }).end();
  assert.equal(tracer.startActiveSpan('e', (span) => (span.end(), 'r1')), 'r1');
  assert.equal(tracer.startActiveSpan('f', { attributes: attrs }, (span) => (span.end(), 'r2')), 'r2');
  assert.equal(tracer.startActiveSpan('g', { attributes: attrs }, api.context.active(), (span) => (span.end(), 'r3')), 'r3');
  const byName = Object.fromEntries(exporter.getFinishedSpans().map((s) => [s.name, s.attributes]));
  for (const name of ['a', 'f', 'g']) assert.equal(byName[name]['db.system'], 'postgresql', name);
  for (const name of ['b', 'c', 'd', 'e']) assert.equal(byName[name]['db.system'], undefined, name);
  assert.deepEqual(attrs, { 'db.system.name': 'postgresql' }, 'the caller attributes are not mutated');
  const stable = tracerOf({ legacyKeys: false, normalizeSpanName: false });
  stable.wrapped.getTracer('t').startSpan('s', { attributes: attrs }).end();
  assert.equal(stable.exporter.getFinishedSpans()[0].attributes['db.system'], undefined);
});

test('R1: an ERROR status keeps its code and drops the description; other statuses unchanged; exception events kept', () => {
  const { exporter, wrapped } = tracerOf({ legacyKeys: false, normalizeSpanName: false });
  const tracer = wrapped.getTracer('t');
  const failed = tracer.startSpan('failed');
  assert.equal(failed.setStatus({ code: api.SpanStatusCode.ERROR, message: "Duplicate entry 'bound-secret-x'" }), failed, 'chainable');
  failed.recordException(new Error('kept'));
  failed.end();
  const ok = tracer.startSpan('ok');
  ok.setStatus({ code: api.SpanStatusCode.OK, message: 'fine' });
  ok.end();
  tracer.startActiveSpan('active', (span) => {
    span.setStatus({ code: api.SpanStatusCode.ERROR, message: 'secret' });
    span.end();
  });
  const byName = Object.fromEntries(exporter.getFinishedSpans().map((s) => [s.name, s]));
  assert.deepEqual(byName.failed.status, { code: api.SpanStatusCode.ERROR });
  assert.equal(byName.failed.attributes['error.type'], '_OTHER', 'N1: classifiable without the text');
  assert.equal(byName.ok.attributes['error.type'], undefined);
  assert.equal(byName.failed.events[0].attributes['exception.message'], 'kept');
  assert.deepEqual(byName.active.status, { code: api.SpanStatusCode.ERROR });
  assert.equal(byName.ok.status.code, api.SpanStatusCode.OK);
  const typed = tracer.startSpan('typed', { attributes: { 'error.type': '23505' } });
  typed.setStatus({ code: api.SpanStatusCode.ERROR, message: 'x' });
  typed.end();
  assert.equal(exporter.getFinishedSpans().find((s) => s.name === 'typed').attributes['error.type'], '23505', 'an existing error.type is kept');
  const frozen = Object.freeze({ setStatus() {} });
  const tp = providers.dbTracerProvider({ getTracer: () => ({ startSpan: () => frozen }) }, { legacyKeys: false, normalizeSpanName: false });
  assert.equal(tp.getTracer('x').startSpan('y'), frozen, 'a frozen span is left as it is');
});

test('obs M3: mysql2 span names from the statement (whitespace, comments, case, length; fallback db.system.name)', () => {
  const table = [
    ['SELECT ? AS v', 'SELECT'],
    ['\n    select *\n    from t', 'SELECT'],
    ['/* tag */ UPDATE t SET v = ?', 'UPDATE'],
    ['-- c\n# d\n  insert into t values (?)', 'INSERT'],
    ['COMMIT', 'COMMIT'],
    ['SELECT(1)', 'SELECT'],
    ['SELECT\t1', 'SELECT'],
    [`${'x'.repeat(50)} 1`, 'X'.repeat(providers.MAX_SPAN_NAME)],
    ['/* never closed', 'mysql'],
    ['-- only a comment', 'mysql'],
    ['   ', 'mysql'],
    ['(SELECT 1)', 'mysql'],
  ];
  for (const [text, want] of table) assert.equal(providers.statementSpanName(text, 'mysql'), want, JSON.stringify(text));
  assert.equal(providers.statementSpanName(undefined, 'mysql'), 'mysql');
  const { exporter, wrapped } = tracerOf({ legacyKeys: false, normalizeSpanName: true });
  const tracer = wrapped.getTracer('t');
  tracer.startSpan('upstream', { attributes: { 'db.system.name': 'mysql', 'db.query.text': '  delete from t' } }).end();
  tracer.startSpan('upstream', { attributes: { 'db.query.text': '' } }).end();
  tracer.startSpan('plain').end();
  tracer.startActiveSpan('upstream', { attributes: { 'db.system.name': 'mysql', 'db.query.text': 'call p()' } }, (s) => s.end());
  assert.deepEqual(exporter.getFinishedSpans().map((s) => s.name), ['DELETE', 'upstream', 'plain', 'CALL']);
});

test('dev L5: wrappers forward the members they do not change (forceFlush, getDelegate, unknown ones)', async () => {
  let flushed = 0;
  const inner = { getTracer: () => ({ startSpan: () => api.trace.wrapSpanContext(api.INVALID_SPAN_CONTEXT), extra: () => 'tracer-extra' }), forceFlush: () => ((flushed += 1), Promise.resolve()), getDelegate: () => 'delegate' };
  const tp = providers.dbTracerProvider(inner, { legacyKeys: false, normalizeSpanName: false });
  await tp.forceFlush();
  assert.equal(flushed, 1);
  assert.equal(tp.getDelegate(), 'delegate');
  assert.equal(tp.getTracer('x').extra(), 'tracer-extra');
  const mp = providers.dbMeterProvider({ getMeter: () => ({ unknownMember: 42 }), forceFlush: () => 'mp-flush' }, { legacyKeys: true, current: () => true });
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
  const meter = providers.dbMeterProvider(provider, { legacyKeys: true, current: () => true }).getMeter('m', '1');
  const attrs = { 'db.system.name': 'mysql' };
  meter.createCounter('c').add(1, attrs);
  meter.createUpDownCounter('u').add(2, attrs);
  meter.createHistogram('h').record(0.5, attrs);
  meter.createGauge('g').record(3, attrs);
  meter.createCounter('plain').add(1);
  const og = meter.createObservableGauge('og');
  og.addCallback((r) => r.observe(1, attrs));
  meter.createObservableCounter('oc').addCallback((r) => r.observe(1));
  meter.createObservableUpDownCounter('ou').addCallback((r) => r.observe(1));
  let batchCalls = 0;
  const batch = (r) => {
    batchCalls++;
    r.observe(og, 2, { k: 'v' });
  };
  meter.addBatchObservableCallback(batch, [og]);
  const { resourceMetrics } = await reader.collect();
  const points = Object.fromEntries(resourceMetrics.scopeMetrics[0].metrics.map((m) => [m.descriptor.name, m.dataPoints.map((p) => p.attributes)]));
  for (const name of ['c', 'u', 'h', 'g']) assert.deepEqual(points[name], [{ 'db.system.name': 'mysql', 'db.system': 'mysql' }], name);
  assert.deepEqual(points.plain, [{}]);
  assert.deepEqual(points.og, [{ 'db.system.name': 'mysql' }, { k: 'v' }], 'observable measurements are not rewritten');
  assert.ok(points.oc && points.ou);
  assert.equal(batchCalls, 1);
  meter.removeBatchObservableCallback(batch, [og]);
  await reader.collect();
  assert.equal(batchCalls, 1, 'batch callback removed');
  await provider.shutdown();
});

test('M1: instruments of an older meter bind record nothing; without legacy keys attributes pass unchanged', async () => {
  const reader = new TestReader();
  const provider = new MeterProvider({ readers: [reader] });
  let generation = 1;
  const old = providers.dbMeterProvider(provider, { legacyKeys: false, current: () => generation === 1 }).getMeter('m');
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

// ── lifecycle through setup({ instrumentations }) ─────────────────────

test('setup() binds and enables it, shutdown() disables it, a re-setup enables it again', async () => {
  const inst = sqlInstrumentation();
  const children = childrenForTests(inst);
  assert.equal(children.length, 2);
  assert.deepEqual(childrenForTests({}), [], 'only results of sqlInstrumentation()');
  assert.ok(children.every((c) => !c.isEnabled()), 'disabled until setup()');
  helper.setup({ instrumentations: [inst] });
  assert.equal(inst.getConfig().enabled, true);
  assert.ok(children.every((c) => c.isEnabled()));
  await helper.shutdown();
  assert.equal(inst.getConfig().enabled, false);
  assert.ok(children.every((c) => !c.isEnabled()));
  helper.setup({ instrumentations: [inst] });
  assert.ok(children.every((c) => c.isEnabled()), 're-enabled by the next setup');
});

test('every result shares the contrib instrumentations; each child is enabled while any result is (L5)', () => {
  const a = sqlInstrumentation();
  const b = sqlInstrumentation({ mysql2: false });
  const [pgChild] = childrenForTests(a);
  assert.equal(childrenForTests(b)[0], pgChild);
  a.enable();
  b.enable();
  b.enable(); // counted once per result
  b.disable();
  assert.equal(pgChild.isEnabled(), true, 'a still enabled');
  a.disable();
  assert.equal(pgChild.isEnabled(), false);
  a.disable(); // idempotent
  assert.equal(pgChild.isEnabled(), false);
});

test('the same TracerProvider again is not re-bound; a meter bind always is (M1)', () => {
  const inst = sqlInstrumentation({ mysql2: false });
  const [child] = childrenForTests(inst);
  const calls = [];
  const original = { t: child.setTracerProvider, m: child.setMeterProvider, l: child.setLoggerProvider };
  child.setTracerProvider = (p) => calls.push(['t', p]);
  child.setMeterProvider = (p) => calls.push(['m', p]);
  child.setLoggerProvider = (p) => calls.push(['l', p]);
  try {
    const tp = new TracerProvider();
    const mp = new MeterProvider();
    inst.setTracerProvider(tp);
    inst.setTracerProvider(tp);
    inst.setMeterProvider(mp);
    inst.setMeterProvider(mp);
    inst.setLoggerProvider('logger');
  } finally {
    Object.assign(child, { setTracerProvider: original.t, setMeterProvider: original.m, setLoggerProvider: original.l });
  }
  assert.deepEqual(calls.map(([k]) => k), ['t', 'm', 'm', 'l']);
  assert.equal(calls[3][1], 'logger');
});

test('L-c: a setMeterProvider that throws restores the previous generation (older instruments keep recording)', async () => {
  const inst = sqlInstrumentation({ mysql2: false });
  const [child] = childrenForTests(inst);
  const reader = new TestReader();
  const provider = new MeterProvider({ readers: [reader] });
  const original = child.setMeterProvider;
  let bound;
  child.setMeterProvider = (p) => {
    if (bound === undefined) bound = p;
    else throw new Error('bind boom');
  };
  try {
    inst.setMeterProvider(provider);
    assert.throws(() => inst.setMeterProvider(provider), /bind boom/);
  } finally {
    child.setMeterProvider = original;
  }
  bound.getMeter('m').createCounter('c').add(1);
  const { resourceMetrics } = await reader.collect();
  assert.equal(resourceMetrics.scopeMetrics[0].metrics[0].dataPoints[0].value, 1);
  await provider.shutdown();
});

test('OTEL_SDK_DISABLED=true leaves it disabled (no instrumentation, as Python)', () => {
  process.env.OTEL_SDK_DISABLED = 'true';
  const inst = sqlInstrumentation();
  helper.setup({ instrumentations: [inst] });
  assert.equal(inst.getConfig().enabled, false);
  assert.ok(childrenForTests(inst).every((c) => !c.isEnabled()));
});
