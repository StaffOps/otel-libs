'use strict';
// D-JS10: one shared state per process; getState() never throws (a preload
// must not crash the app) and goes inactive on anything unexpected.
const test = require('node:test');
const assert = require('node:assert/strict');
const { runCjs, lastJson } = require('./helpers.cjs');
const { getState, STATE_KEY, reset, setDiagnosticSink } = require('@staffops/otel-helper/testing');

const KEY = "Symbol.for('staffops.otel-helper.state.v1')";
// Runs `pre` (which prepares the slot), then getState() twice; prints both results.
function probe(pre) {
  return runCjs(`
    ${pre}
    const { getState } = require('@staffops/otel-helper/testing');
    const a = getState();
    const b = getState();
    console.log(JSON.stringify({ a: a === null ? null : 'state', same: a === b }));
  `);
}
const INACTIVE = /^otelhelper: .*; this copy of @staffops\/otel-helper stays inactive.*\n$/;

test('state lives on Symbol.for("staffops.otel-helper.state.v1") and records the version', () => {
  assert.equal(STATE_KEY, Symbol.for('staffops.otel-helper.state.v1'));
  const state = getState();
  assert.equal(state, globalThis[STATE_KEY]);
  assert.equal(state.version, require('../package.json').version);
  assert.equal(getState(), state, 'resolved once and reused');
});

test('the slot is a non-enumerable, non-writable, non-configurable symbol property', () => {
  getState();
  assert.ok(Object.getOwnPropertySymbols(globalThis).includes(STATE_KEY));
  assert.equal(Object.keys(globalThis).includes(String(STATE_KEY)), false);
  const desc = Object.getOwnPropertyDescriptor(globalThis, STATE_KEY);
  assert.deepEqual([desc.enumerable, desc.writable, desc.configurable], [false, false, false]);
});

test('version is non-writable; the slot survives a delete attempt', () => {
  const state = getState();
  assert.throws(() => { 'use strict'; state.version = '9.9.9'; }, TypeError);
  assert.throws(() => { delete globalThis[STATE_KEY]; }, TypeError);
  assert.equal(globalThis[STATE_KEY], state);
});

test('a state left by a different version keeps this copy inactive and reports once', () => {
  const res = probe(`Object.defineProperty(globalThis, ${KEY}, { value: { version: '9.9.9', hooks: undefined } });`);
  assert.deepEqual(lastJson(res), { a: null, same: true });
  assert.match(res.stderr, /^otelhelper: another version is already loaded in this process; this copy of @staffops\/otel-helper stays inactive loaded=9\.9\.9 inactive=\S+\n$/);
});

for (const [name, pre] of [
  ['null slot', `globalThis[${KEY}] = null;`],
  ['non-object slot', `globalThis[${KEY}] = 42;`],
  ['forged version (not a string)', `globalThis[${KEY}] = { version: 42 };`],
  ['version getter that throws', `globalThis[${KEY}] = { get version() { throw new Error('boom'); } };`],
]) {
  test(`unexpected slot value (${name}): inactive, one report, no throw`, () => {
    const res = probe(pre);
    assert.equal(res.status, 0, res.stderr);
    assert.deepEqual(lastJson(res), { a: null, same: true });
    assert.match(res.stderr, /^otelhelper: the shared state slot holds an unexpected value; this copy/);
    assert.match(res.stderr, INACTIVE);
  });
}

test('a slot whose getter throws: inactive, one report, no throw', () => {
  const res = probe(`Object.defineProperty(globalThis, ${KEY}, { get() { throw new Error('boom'); } });`);
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(lastJson(res), { a: null, same: true });
  assert.match(res.stderr, /^otelhelper: the shared state slot cannot be read; this copy/);
});

test('a non-extensible globalThis: inactive, one report, no throw', () => {
  const res = probe('Object.preventExtensions(globalThis);');
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(lastJson(res), { a: null, same: true });
  assert.match(res.stderr, /^otelhelper: the shared state slot cannot be created; this copy/);
});

test('a frozen same-version state is adopted; writes to it are reported, never thrown', () => {
  const res = runCjs(`
    const { VERSION } = require('@staffops/otel-helper');
    const frozen = Object.freeze({ version: VERSION, hooks: undefined, sink: undefined });
    Object.defineProperty(globalThis, ${KEY}, { value: frozen });
    const t = require('@staffops/otel-helper/testing');
    const adopted = t.getState() === frozen;
    t.setDiagnosticSink(() => {});
    const { installHooks, currentEnvironment } = require('./dist/internal/hooks.js');
    const env = { ...currentEnvironment(), supportsSyncHooks: () => false, evalMode: false };
    const mode = installHooks(env);
    const again = installHooks(env);
    console.log(JSON.stringify({ adopted, mode, again }));
  `);
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(lastJson(res), { adopted: true, mode: 'async', again: 'async' });
  const lines = res.stderr.split('\n').filter((l) => l.startsWith('otelhelper: '));
  assert.deepEqual(lines, [
    'otelhelper: the diagnostics sink cannot be replaced: the shared state is read-only',
    'otelhelper: the hooks mode cannot be recorded: the shared state is read-only hooks=async',
  ]);
});

test('a same-version state is adopted, not replaced', () => {
  const res = runCjs(`
    const pre = { version: require('@staffops/otel-helper').VERSION, hooks: undefined, sink: undefined };
    Object.defineProperty(globalThis, ${KEY}, { value: pre });
    console.log(JSON.stringify({ same: require('@staffops/otel-helper/testing').getState() === pre }));
  `);
  assert.deepEqual(lastJson(res), { same: true });
  assert.equal(res.stderr, '');
});

test('setDiagnosticSink() is a no-op while this copy is inactive', () => {
  const res = runCjs(`
    Object.defineProperty(globalThis, ${KEY}, { value: { version: '9.9.9' } });
    const t = require('@staffops/otel-helper/testing');
    t.setDiagnosticSink(() => { throw new Error('must not be installed'); });
  `);
  assert.equal(res.status, 0);
  assert.match(res.stderr, /another version is already loaded/);
});

test('reset() restores the default diagnostics sink', () => {
  const lines = [];
  setDiagnosticSink((l) => lines.push(l));
  reset();
  const diag = require('../dist/internal/diagnostics.js');
  const orig = process.stderr.write;
  const captured = [];
  process.stderr.write = (chunk) => { captured.push(String(chunk)); return true; };
  try {
    diag.report('after reset');
  } finally {
    process.stderr.write = orig;
  }
  assert.deepEqual(lines, []);
  assert.deepEqual(captured, ['otelhelper: after reset\n']);
});
