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
  assert.equal(diag.formatDiagnostic('m', { v: 'a\u007fb' }), 'otelhelper: m v="a\\u007fb"');
  assert.equal(diag.formatDiagnostic('m', { v: 'a\u0085b\u2028c\u2029d\u009f' }), 'otelhelper: m v="a\\u0085b\\u2028c\\u2029d\\u009f"');
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

test('the sink receives {origin, level}: a helper warning by default, else what the caller passes', () => {
  const records = [];
  setDiagnosticSink((line, meta) => records.push([line, meta]));
  diag.report('own');
  diag.report('forwarded', { n: 1 }, { origin: 'diag', level: 'debug' });
  diag.report('fatal', undefined, { origin: 'helper', level: 'error' });
  assert.deepEqual(records, [
    ['otelhelper: own', { origin: 'helper', level: 'warn' }],
    ['otelhelper: forwarded n=1', { origin: 'diag', level: 'debug' }],
    ['otelhelper: fatal', { origin: 'helper', level: 'error' }],
  ]);
  assert.ok(Object.isFrozen(records[0][1]), 'the default metadata is shared and frozen');
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

test('B9 fix round: redactErrorText drops URL userinfo, query and fragment, credential pairs and bearer tokens', () => {
  assert.equal(diag.redactErrorText('connect https://user:pw@collector.example.com:4318/v1/traces?sig=1#f failed'), 'connect https://collector.example.com:4318/v1/traces failed');
  assert.equal(diag.redactErrorText('a http://h.example.com b'), 'a http://h.example.com b');
  assert.equal(diag.redactErrorText('x grpc://u@h.example.com?k=v'), 'x grpc://h.example.com');
  assert.equal(diag.redactErrorText('x https://h.example.com/p@ss'), 'x https://h.example.com');
  assert.equal(diag.redactErrorText('Authorization: Bearer abc.def'), 'Authorization: <redacted> <redacted>');
  assert.equal(diag.redactErrorText('api-key=k1, token="t 2"; password: p3'), 'api-key=<redacted>, token=<redacted>; password: <redacted>');
  assert.equal(diag.redactErrorText('basic dXNlcjpwYXNz'), 'basic <redacted>');
  assert.equal(diag.redactErrorText('y'.repeat(100)), `${'y'.repeat(64)}…`);
});

test('B9 final round: redactErrorText key names, generic credential names, schemeless userinfo, dns:///, bearer forms, basic', () => {
  const r = diag.redactErrorText;
  assert.equal(r('xtoken=keep'), 'xtoken=<redacted>'); // generic rule: a name containing "token"
  assert.equal(r('"password":"p1"'), '"password":<redacted>');
  assert.equal(r('"api_key": "k"'), '"api_key": <redacted>');
  assert.equal(r('X-Team-Id=t1 my_secret_value: s'), 'X-Team-Id=<redacted> my_secret_value: <redacted>');
  assert.equal(r('client_auth=a; session-cookie=c'), 'client_auth=<redacted>; session-cookie=<redacted>');
  assert.equal(r('dial user:pw@collector.example.com:4317 failed'), 'dial collector.example.com:4317 failed');
  assert.equal(r('dns:///user:pw@collector.example.com:4317'), 'dns:///collector.example.com:4317');
  assert.equal(r('bearer:abc'), 'bearer:<redacted>');
  assert.equal(r('Bearer=abc'), 'Bearer=<redacted>');
  assert.equal(r('bearerabc'), 'bearer <redacted>');
  assert.equal(r('basic auth failed'), 'basic auth failed');
  assert.equal(r('Basic dXNlcjpw=='), 'Basic <redacted>');
  assert.equal(r('basic short'), 'basic short');
  assert.equal(r('the error: x@y'), 'the error: x@y');
  // Large inputs stay linear (no ReDoS).
  for (const big of ['a'.repeat(200_000), '_'.repeat(200_000), 'k'.repeat(200_000) + '=', `${'x:'.repeat(100_000)}@`, `https://${'u'.repeat(200_000)}`]) {
    const started = Date.now();
    r(big);
    assert.ok(Date.now() - started < 1000, `slow on ${big.slice(0, 8)}…`);
  }
});
