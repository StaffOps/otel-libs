'use strict';
// D-JS11: /hooks installs import-in-the-middle sync hooks when the running Node
// supports them, else the async module.register loader; once per process;
// never under --eval/--print with sync hooks (spike-B1c.md F1).
const test = require('node:test');
const assert = require('node:assert/strict');
const { runNode, lastJson } = require('./helpers.cjs');

const report = 'test/fixtures/report-hooks.cjs';
const MAJOR = Number.parseInt(process.versions.node, 10);
const REGISTER_DEPRECATED = MAJOR >= 26;

// Node 26 deprecates module.register() (DEP0205). Production only takes the
// async path where supportsSyncHooks() is false (22 < 22.22.3, 24 < 24.11.1)
// or under --eval before 26; the forced-async tests below tolerate it.
const withoutDep0205 = (stderr) =>
  stderr.split('\n').filter((l) => l && !/\[DEP0205\]|--trace-deprecation/.test(l)).join('\n');

for (const [flag, entry] of [
  ['--require', '@staffops/otel-helper/hooks'],
  ['--import', '@staffops/otel-helper/hooks'],
  ['--require', '@staffops/otel-helper/register'],
  ['--import', '@staffops/otel-helper/register'],
]) {
  test(`${flag} ${entry} installs the hooks for this Node (${process.versions.node})`, () => {
    const res = runNode([flag, entry, report]);
    const out = lastJson(res);
    assert.equal(out.hooks, out.expected);
    assert.equal(res.stderr, '', 'no warnings (require(esm), iitm double registration)');
  });
}

test('loading /hooks through both formats installs them once, without warnings', () => {
  const res = runNode(['--require', '@staffops/otel-helper/hooks', '--import', '@staffops/otel-helper/hooks', report]);
  const out = lastJson(res);
  assert.equal(out.hooks, out.expected);
  assert.equal(res.stderr, '');
});

// F1: iitm sync hooks + --eval/--print crash (ENOENT 'module'). Container
// HEALTHCHECKs (`NODE_OPTIONS=--require …/register` + `node -e`) hit this.
const evalExpected = REGISTER_DEPRECATED ? 'cjs-only' : 'async';
const hooksOf = "globalThis[Symbol.for('staffops.otel-helper.state.v1')].hooks";
for (const [name, args, env] of [
  ['-r /register -e', ['-r', '@staffops/otel-helper/register', '-e', `console.log(JSON.stringify({ hooks: ${hooksOf} }))`]],
  ['-r /hooks -p', ['-r', '@staffops/otel-helper/hooks', '-p', `JSON.stringify({ hooks: ${hooksOf} })`]],
  ['NODE_OPTIONS=--require=/register + -e', ['-e', `console.log(JSON.stringify({ hooks: ${hooksOf} }))`],
    { NODE_OPTIONS: '--require=@staffops/otel-helper/register' }],
  ['--import /register -e', ['--import', '@staffops/otel-helper/register', '-e', `console.log(JSON.stringify({ hooks: ${hooksOf} }))`]],
]) {
  test(`${name} runs (no sync hooks under eval): hooks=${evalExpected}`, () => {
    const res = runNode(args, { env });
    assert.equal(res.status, 0, res.stderr);
    assert.deepEqual(lastJson(res), { hooks: evalExpected });
    const lines = res.stderr.split('\n').filter(Boolean);
    if (REGISTER_DEPRECATED) {
      assert.equal(lines.length, 1, res.stderr);
      assert.match(lines[0], /^otelhelper: ESM import hooks not installed under --eval\/--print on this Node; only require\(\) is instrumented node=/);
    } else {
      assert.deepEqual(lines, []);
    }
  });
}

test('an ESM import still works after the async loader under --eval (Node < 26)', { skip: REGISTER_DEPRECATED }, () => {
  const res = runNode(['-r', '@staffops/otel-helper/hooks', '-e',
    "import('node:path').then((p) => console.log(JSON.stringify({ ok: typeof p.join === 'function' })))"]);
  assert.deepEqual(lastJson(res), { ok: true });
  assert.equal(res.stderr, '');
});

test('installHooks() is a no-op when another version owns the state', () => {
  const res = runNode(['-e', `
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'), { value: { version: '0.0.0-other', hooks: undefined } });
    const { installHooks } = require('./dist/internal/hooks.js');
    console.log(JSON.stringify({ mode: installHooks() ?? null }));
  `]);
  assert.deepEqual(lastJson(res), { mode: null });
  assert.match(res.stderr, /another version is already loaded/);
});

test('the async module.register path works on every supported Node', () => {
  const res = runNode(['test/fixtures/force-async-hooks.cjs']);
  assert.deepEqual(lastJson(res), { mode: 'async', again: 'async', imported: true });
  assert.equal(withoutDep0205(res.stderr), '');
});

test('a --require preload registers the async loader once (skipped in the hooks thread)', () => {
  const res = runNode(['--require', './test/fixtures/force-async-preload.cjs', 'test/fixtures/report-preload.cjs']);
  assert.deepEqual(lastJson(res), { mode: 'async', imported: true });
  assert.equal(withoutDep0205(res.stderr), '', 'no "hook has already been initialized" warning');
});

test('installHooks() does nothing inside an internal thread', () => {
  const res = runNode(['-e', `
    const { installHooks, currentEnvironment } = require('./dist/internal/hooks.js');
    const mode = installHooks({ ...currentEnvironment(), internalThread: true });
    console.log(JSON.stringify({ mode: mode ?? null, state: globalThis[Symbol.for('staffops.otel-helper.state.v1')] ?? null }));
  `]);
  assert.deepEqual(lastJson(res), { mode: null, state: null });
});

test('a hook installation error is reported, never thrown', () => {
  const res = runNode(['-e', `
    const { installHooks, currentEnvironment } = require('./dist/internal/hooks.js');
    const mode = installHooks({ ...currentEnvironment(), evalMode: false, supportsSyncHooks: () => { throw new RangeError('probe failed'); } });
    console.log(JSON.stringify({ mode: mode ?? null }));
  `]);
  assert.equal(res.status, 0);
  assert.deepEqual(lastJson(res), { mode: null });
  assert.equal(res.stderr, 'otelhelper: module hooks could not be installed; ESM imports are not instrumented error="RangeError: probe failed"\n');
});

test('a non-Error throw is reported too', () => {
  const res = runNode(['-e', `
    const { installHooks, currentEnvironment } = require('./dist/internal/hooks.js');
    installHooks({ ...currentEnvironment(), evalMode: false, supportsSyncHooks: () => { throw 'plain'; } });
  `]);
  assert.equal(res.stderr, 'otelhelper: module hooks could not be installed; ESM imports are not instrumented error=plain\n');
});

test('runRegister() is skipped in an internal thread (B9 setup must not run there)', () => {
  const res = runNode(['-e', `
    const { runRegister } = require('./dist/internal/register.js');
    const ran = runRegister(true);
    console.log(JSON.stringify({ ran, state: globalThis[Symbol.for('staffops.otel-helper.state.v1')] ?? null }));
  `]);
  assert.deepEqual(lastJson(res), { ran: false, state: null });
});

test('eval mode where module.register is deprecated installs no ESM hook (cjs-only), one line', () => {
  const res = runNode(['-e', `
    const { installHooks, currentEnvironment } = require('./dist/internal/hooks.js');
    const env = { ...currentEnvironment(), evalMode: true, registerDeprecated: true };
    console.log(JSON.stringify({ mode: installHooks(env), again: installHooks(env) }));
  `]);
  assert.deepEqual(lastJson(res), { mode: 'cjs-only', again: 'cjs-only' });
  assert.match(res.stderr, /^otelhelper: ESM import hooks not installed under --eval\/--print on this Node; only require\(\) is instrumented node=\S+\n$/);
});

test('a same-version state whose hooks getter throws still gets the hooks installed', () => {
  const res = runNode(['-e', `
    const s = { version: require('@staffops/otel-helper').VERSION, sink: undefined };
    let stored;
    Object.defineProperty(s, 'hooks', { get() { throw new Error('boom'); }, set(v) { stored = v; } });
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'), { value: s });
    const { installHooks, currentEnvironment } = require('./dist/internal/hooks.js');
    const mode = installHooks({ ...currentEnvironment(), supportsSyncHooks: () => false, evalMode: false });
    console.log(JSON.stringify({ mode, stored }));
  `]);
  assert.deepEqual(lastJson(res), { mode: 'async', stored: 'async' });
  assert.equal(withoutDep0205(res.stderr), '');
});
