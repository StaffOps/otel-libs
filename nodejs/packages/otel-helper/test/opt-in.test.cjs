'use strict';
// setup({ instrumentations }) — the opt-in instrumentations (task B10a; spec
// JS8, design D-JS7/D-JS10) — and the OTEL_SEMCONV_STABILITY_OPT_IN HTTP
// warning (design D-JS9). Real pg/mysql2 instrumentation: the -sql package's
// own tests; here a fake Instrumentation records what the core does with it.
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('@opentelemetry/api');
const { TracerProvider } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { setupFault } = require('../dist/internal/setup.js');
const { runCjs, lastJson } = require('./helpers.cjs');

let records;
let saved;
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

/** A fake Instrumentation that records the calls the core makes; `fail` names a method that throws. */
function fake(name = 'fake', { fail, withLogger = true } = {}) {
  const calls = [];
  const inst = {
    instrumentationName: name,
    instrumentationVersion: '1.0.0',
    enabled: false,
    calls,
    enable() {
      calls.push('enable');
      if (fail === 'enable') throw new Error('enable boom');
      this.enabled = true;
    },
    disable() {
      calls.push('disable');
      if (fail === 'disable') throw new Error('disable boom');
      this.enabled = false;
    },
    setTracerProvider(p) {
      calls.push('tracer');
      if (fail === 'tracer') throw new Error('tracer boom');
      this.tracerProvider = p;
    },
    setMeterProvider(p) {
      calls.push('meter');
      this.meterProvider = p;
    },
    setConfig() {},
    getConfig: () => ({}),
  };
  if (withLogger) {
    inst.setLoggerProvider = function (p) {
      calls.push('logger');
      this.loggerProvider = p;
    };
  }
  return inst;
}

test('instrumentations: invalid values throw a TypeError naming the option (R8)', () => {
  for (const [value, message] of [
    ['x', /instrumentations must be an array, got string/],
    [{}, /instrumentations must be an array, got object/],
    [[{}], /instrumentations must contain Instrumentation objects/],
    [[{ ...fake(), instrumentationName: 7 }], /instrumentations must contain Instrumentation objects/],
    [[{ ...fake(), enable: undefined }], /instrumentations must contain Instrumentation objects/],
    [[null], /instrumentations must contain Instrumentation objects/],
  ]) {
    assert.throws(() => helper.setup({ instrumentations: value }), { name: 'TypeError', message }, JSON.stringify(value));
  }
});

test('setup() binds each to the helper forwarding providers and enables it; shutdown() disables it', async () => {
  const a = fake('a');
  const b = fake('b', { withLogger: false });
  helper.setup({ instrumentations: [a, b, a] });
  assert.deepEqual(a.calls, ['tracer', 'meter', 'logger', 'enable'], 'bound before enabled; a duplicate enabled once');
  assert.deepEqual(b.calls, ['tracer', 'meter', 'enable'], 'setLoggerProvider is optional');
  assert.equal(a.tracerProvider, api.trace.getTracerProvider().getDelegate(), 'the forwarding TracerProvider (the global proxy delegate)');
  assert.equal(a.meterProvider, api.metrics.getMeterProvider());
  assert.equal(a.loggerProvider, api.logs.getLoggerProvider());
  await helper.shutdown();
  assert.equal(a.enabled, false);
  assert.equal(b.enabled, false);
  helper.setup({ instrumentations: [b] });
  assert.equal(b.enabled, true, 'enabled again by the next setup');
  assert.equal(a.enabled, false, 'one left out of the next setup stays disabled');
});

test('one that throws while binding or enabling is reported, disabled and left out; the others and setup() carry on', () => {
  const bad = fake('bad-<x>', { fail: 'enable' });
  const badBind = fake('bad-bind', { fail: 'tracer' });
  const good = fake('good');
  helper.setup({ instrumentations: [bad, badBind, good] });
  assert.equal(good.enabled, true);
  assert.equal(bad.enabled, false);
  assert.ok(bad.calls.includes('disable') && badBind.calls.includes('disable'));
  const reported = records.filter((l) => l.includes('opt-in instrumentation could not be enabled'));
  assert.equal(reported.length, 2, records.join('\n'));
  assert.match(reported[0], /instrumentation=bad-<x> error="?enable boom/);
  // The error text is redacted like every helper diagnostic (L6).
  assert.match(reported[1], /instrumentation=bad-bind error="?tracer boom/);
});

test('a disable() that throws is ignored at shutdown', async () => {
  const inst = fake('d', { fail: 'disable' });
  helper.setup({ instrumentations: [inst] });
  await helper.shutdown();
  assert.ok(inst.calls.includes('disable'));
});

test('OTEL_SDK_DISABLED=true: not bound, disabled (one built enabled is turned off)', () => {
  process.env.OTEL_SDK_DISABLED = 'true';
  const inst = fake();
  inst.enabled = true;
  helper.setup({ instrumentations: [inst] });
  assert.deepEqual(inst.calls, ['disable']);
  assert.equal(inst.enabled, false);
});

test('a foreign TracerProvider: bound to it, as the core instrumentations', () => {
  const foreign = new TracerProvider();
  api.trace.setGlobalTracerProvider(foreign);
  try {
    const inst = fake();
    helper.setup({ instrumentations: [inst] });
    assert.equal(inst.tracerProvider, api.trace.getTracerProvider());
    assert.equal(inst.tracerProvider.getDelegate(), foreign);
    assert.equal(inst.enabled, true);
  } finally {
    api.trace.disable();
  }
});

test('G1: a setup failing after the instrumentations step disables them again', () => {
  const inst = fake();
  setupFault.at = (s) => {
    if (s === 'listener') throw new Error('injected');
  };
  try {
    assert.throws(() => helper.setup({ instrumentations: [inst] }), /injected/);
  } finally {
    setupFault.at = undefined;
  }
  assert.deepEqual(inst.calls, ['tracer', 'meter', 'logger', 'enable', 'disable']);
  assert.equal(inst.enabled, false);
});

test('/testing reset() disables every opt-in instrumentation a setup enabled', () => {
  const inst = fake();
  helper.setup({ instrumentations: [inst] });
  testing.reset();
  assert.equal(inst.enabled, false);
});

test('setup() again while active: new instrumentations are turned off and named; the active ones stay on (M3)', () => {
  const active = fake('active');
  helper.setup({ instrumentations: [active] });
  const late = fake('late-one');
  late.enabled = true; // as a contrib constructor leaves it
  helper.setup({ instrumentations: [active, late] });
  assert.deepEqual(late.calls, ['disable']);
  assert.equal(late.enabled, false);
  assert.equal(active.enabled, true, 'the active setup keeps its own');
  assert.ok(records.some((l) => l.includes('setup() already ran') && l.includes('instrumentations=late-one')), records.join('\n'));
});

test('G1: a setup failing before the instrumentations step turns off every opt-in one (M3)', () => {
  const inst = fake();
  inst.enabled = true;
  setupFault.at = (s) => {
    if (s === 'providers') throw new Error('injected');
  };
  try {
    assert.throws(() => helper.setup({ instrumentations: [inst] }), /injected/);
  } finally {
    setupFault.at = undefined;
  }
  assert.deepEqual(inst.calls, ['disable']);
  assert.equal(inst.enabled, false);
});

test('instrumentations: two for one library, or one the core installs, are rejected by name (M2)', () => {
  const sameName = [fake('@opentelemetry/instrumentation-mysql2'), fake('@opentelemetry/instrumentation-mysql2')];
  assert.throws(() => helper.setup({ instrumentations: sameName }), {
    name: 'TypeError',
    message: /two instrumentations for "@opentelemetry\/instrumentation-mysql2"/,
  });
  const composite = { ...fake('@staffops/otel-helper-sql'), coveredInstrumentations: ['@opentelemetry/instrumentation-pg', '@opentelemetry/instrumentation-mysql2'] };
  assert.throws(() => helper.setup({ instrumentations: [composite, fake('@opentelemetry/instrumentation-mysql2')] }), {
    message: /two instrumentations for "@opentelemetry\/instrumentation-mysql2" \("@staffops\/otel-helper-sql" and "@opentelemetry\/instrumentation-mysql2"\)/,
  });
  for (const name of ['@opentelemetry/instrumentation-http', '@opentelemetry/instrumentation-undici', '@opentelemetry/instrumentation-express', '@opentelemetry/instrumentation-grpc']) {
    assert.throws(() => helper.setup({ instrumentations: [fake(name)] }), { message: /which the helper installs itself/ }, name);
  }
  assert.throws(() => helper.setup({ instrumentations: [{ ...fake('x'), coveredInstrumentations: 'pg' }] }), {
    message: /coveredInstrumentations that is not an array of strings/,
  });
  helper.setup({ instrumentations: [composite, fake('other')] });
  assert.equal(composite.enabled, true);
});

const FAKE_SRC = `{ instrumentationName: 'x', instrumentationVersion: '1', enabled: true, enable() { this.enabled = true; }, disable() { this.enabled = false; },
  setTracerProvider() {}, setMeterProvider() {}, setConfig() {}, getConfig() { return {}; } }`;

test('another API version\'s registry: opt-in ones are turned off', () => {
  const res = runCjs(`
    globalThis[Symbol.for('opentelemetry.js.api.1')] = { version: '1.9.0' };
    const inst = ${FAKE_SRC};
    require('@staffops/otel-helper').setup({ instrumentations: [inst] });
    console.log(JSON.stringify({ enabled: inst.enabled }));`);
  assert.deepEqual(lastJson(res), { enabled: false });
});

test('an inactive copy (another helper version owns the state): opt-in ones are turned off (M3)', () => {
  const res = runCjs(`
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'), { value: { version: '9.9.9', hooks: undefined } });
    const inst = ${FAKE_SRC};
    require('@staffops/otel-helper').setup({ instrumentations: [inst] });
    console.log(JSON.stringify({ enabled: inst.enabled }));`);
  assert.deepEqual(lastJson(res), { enabled: false });
});
