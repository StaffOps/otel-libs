'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const diag = require('../dist/internal/diagnostics.js');
const { setDiagnosticSink } = require('@staffops/otel-helper/testing');
const { runCjs } = require('./helpers.cjs');

test.afterEach(() => setDiagnosticSink(undefined));

test('formats message and key=value attributes, quoting only when needed', () => {
  assert.equal(diag.formatDiagnostic('hello'), 'otelhelper: hello');
  assert.equal(
    diag.formatDiagnostic('bad value', { var: 'OTEL_X', got: 'a b', n: 3, ok: false, empty: '', q: 'x"y', eq: 'k=v', 'a.b_c': 'x' }),
    'otelhelper: bad value var=OTEL_X got="a b" n=3 ok=false empty="" q="x\\"y" eq="k=v" a.b_c=x',
  );
});

test('control characters, DEL and backslashes are quoted and escaped (no forged records)', () => {
  assert.equal(diag.formatDiagnostic('m', { v: 'a\nb' }), 'otelhelper: m v="a\\nb"');
  assert.equal(diag.formatDiagnostic('m', { v: 'a\u0000b' }), 'otelhelper: m v="a\\u0000b"');
  assert.equal(diag.formatDiagnostic('m', { v: 'a\u007fb' }), `otelhelper: m v=${JSON.stringify('a\u007fb')}`);
  assert.equal(diag.formatDiagnostic('m', { v: 'a\\b' }), 'otelhelper: m v="a\\\\b"');
});

test('attribute keys outside [A-Za-z0-9_.] are a programming error', () => {
  for (const key of ['a b', 'a=b', '', 'k\n', 'é']) {
    assert.throws(() => diag.formatDiagnostic('m', { [key]: 1 }), TypeError, JSON.stringify(key));
  }
});

test('report() writes through the sink installed on the shared state', () => {
  const lines = [];
  setDiagnosticSink((l) => lines.push(l));
  diag.report('m', { a: 1 });
  assert.deepEqual(lines, ['otelhelper: m a=1']);
});

test('the default sink writes one line to stderr, nothing to stdout', () => {
  const res = runCjs("require('./dist/internal/diagnostics.js').report('to stderr', { k: 'v' })");
  assert.equal(res.status, 0);
  assert.equal(res.stdout, '');
  assert.equal(res.stderr, 'otelhelper: to stderr k=v\n');
});

test('a throwing custom sink falls back to stderr and never throws', () => {
  const res = runCjs(`
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => { throw new Error('boom'); });
    require('./dist/internal/diagnostics.js').report('still visible');
  `);
  assert.equal(res.status, 0);
  assert.equal(res.stderr, 'otelhelper: still visible\n');
});

test('a second copy of the same version reports through the hooked sink', () => {
  const res = runCjs(`
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
    // Simulate a second install of the same version: fresh module instances.
    for (const k of Object.keys(require.cache)) if (k.includes('/otel-helper/dist/')) delete require.cache[k];
    require('./dist/internal/diagnostics.js').report('from copy two');
    console.log(JSON.stringify(lines));
  `);
  assert.deepEqual(JSON.parse(res.stdout.trim()), ['otelhelper: from copy two']);
  assert.equal(res.stderr, '');
});

test('a sink of another version is never used', () => {
  const res = runCjs(`
    const lines = [];
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'),
      { value: { version: '0.0.0-other', hooks: undefined, sink: (l) => lines.push(l) } });
    require('./dist/internal/diagnostics.js').report('to stderr');
    console.log(JSON.stringify(lines));
  `);
  assert.deepEqual(JSON.parse(res.stdout.trim()), []);
  assert.equal(res.stderr, 'otelhelper: to stderr\n');
});

test('an unreadable sink falls back to stderr', () => {
  const res = runCjs(`
    const { VERSION } = require('@staffops/otel-helper');
    const s = { version: VERSION, hooks: undefined };
    Object.defineProperty(s, 'sink', { get() { throw new Error('boom'); } });
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'), { value: s });
    require('./dist/internal/diagnostics.js').report('still on stderr');
  `);
  assert.equal(res.status, 0);
  assert.equal(res.stderr, 'otelhelper: still on stderr\n');
});
