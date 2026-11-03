'use strict';
// D-JS11: /hooks installs import-in-the-middle sync hooks when the running Node
// supports them, else the async module.register loader; once per process;
// never under --eval/--print with sync hooks (spike-B1c.md F1).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { runNode, lastJson } = require('./helpers.cjs');

const report = 'test/fixtures/report-hooks.cjs';
const MAJOR = Number.parseInt(process.versions.node, 10);
const REGISTER_DEPRECATED = MAJOR >= 26;

// Node 26 deprecates module.register() (DEP0205). Production only takes the
// async path where supportsSyncHooks() is false (22 < 22.22.3, 24 < 24.11.1)
// or under --eval before 26; the forced-async tests below tolerate it.
const withoutDep0205 = (stderr) =>
  stderr.split('\n').filter((l) => l && !/\[DEP0205\]|--trace-deprecation/.test(l)).join('\n');

// /register calls setup() from the environment (B9): an identity, no /metrics
// listener (never bind :9464 from unit tests), so it writes nothing on stderr.
const QUIET_SETUP = { OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=DEV', OTEL_HELPER_METRICS_PORT: '0' };

for (const [flag, entry] of [
  ['--require', '@staffops/otel-helper/hooks'],
  ['--import', '@staffops/otel-helper/hooks'],
  ['--require', '@staffops/otel-helper/register'],
  ['--import', '@staffops/otel-helper/register'],
]) {
  test(`${flag} ${entry} installs the hooks for this Node (${process.versions.node})`, () => {
    const res = runNode([flag, entry, report], { env: QUIET_SETUP });
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
    const res = runNode(args, { env: { ...QUIET_SETUP, ...env } });
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

// ── B9: /register calls setup(); the ESM-without-hooks warning (spec JS5) ──

test('/register runs setup() from the environment: a later setup() returns its result', () => {
  for (const flag of ['--require', '--import']) {
    const res = runNode([flag, '@staffops/otel-helper/register', '-e', `
      const h = require('@staffops/otel-helper');
      const first = h.setup();
      const api = require('@opentelemetry/api');
      console.log(JSON.stringify({ name: first.serviceName, same: h.setup() === first, tracer: api.trace.getTracerProvider().getDelegate().constructor.name }));
    `], { env: QUIET_SETUP });
    assert.equal(res.status, 0, res.stderr);
    assert.deepEqual(lastJson(res), { name: 'svc', same: true, tracer: 'ForwardingTracerProvider' }, flag);
  }
});

test('a failing setup() under /register is reported, never thrown, and leaves nothing installed', () => {
  const res = runNode(['-e', `
    require('./dist/internal/setup.js').setupFault.at = (step) => { if (step === 'providers') throw new Error('injected at ' + step); };
    const { runRegister } = require('./dist/internal/register.js');
    const ran = runRegister(false);
    const reg = globalThis[Symbol.for('opentelemetry.js.api.1')] ?? {};
    console.log(JSON.stringify({ ran, globals: ['trace', 'metrics', 'logs', 'context', 'propagation'].filter((k) => reg[k] !== undefined) }));
  `], { env: QUIET_SETUP });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(lastJson(res), { ran: true, globals: [] });
  // Under -e on Node >= 26 the hooks entry adds its cjs-only line first (tested above).
  const lines = withoutDep0205(res.stderr).split('\n').filter((l) => l && !l.includes('ESM import hooks not installed under --eval'));
  assert.deepEqual(lines, ['otelhelper: setup() from @staffops/otel-helper/register failed; telemetry is not installed error="injected at providers"']);
});

const ESM_FIXTURE = 'test/fixtures/esm-setup.mjs';
const ESM_WARNING = 'setup() runs in an ES module process without the module hooks';

test('JS5 setup() in an ES module process without the hooks warns once; with the hooks or in CommonJS it does not', () => {
  const without = lastJson(runNode([ESM_FIXTURE], { env: QUIET_SETUP }));
  assert.equal(without.warnings.length, 1, JSON.stringify(without));
  assert.equal(without.resetup, 'svc', 'shutdown() → setup() works and the warning stays once per process');
  assert.equal(
    without.warnings[0],
    'otelhelper: setup() runs in an ES module process without the module hooks; packages imported after setup() (express, @grpc/grpc-js) are not instrumented — start the process with --import @staffops/otel-helper/register, or import @staffops/otel-helper/hooks first in the preload that calls setup()',
  );
  assert.deepEqual(lastJson(runNode(['--import', '@staffops/otel-helper/hooks', ESM_FIXTURE], { env: QUIET_SETUP })).warnings, []);
  const cjs = runNode(['-e', `
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
    require('@staffops/otel-helper').setup();
    console.log(JSON.stringify({ warnings: lines.filter((l) => l.includes(${JSON.stringify(ESM_WARNING)})) }));
  `], { env: QUIET_SETUP });
  assert.deepEqual(lastJson(cjs).warnings, []);
  // OTEL_SDK_DISABLED=true: nothing is instrumented, so nothing to warn about.
  assert.deepEqual(lastJson(runNode([ESM_FIXTURE], { env: { ...QUIET_SETUP, OTEL_SDK_DISABLED: 'true' } })).warnings, []);
});

test('JS5 entryIsEsm: eval input type, extensions, the package type and the default-type flag', () => {
  const { entryIsEsm } = require('../dist/internal/hooks.js');
  const fixtures = path.join(__dirname, 'fixtures');
  const cases = [
    [{ script: undefined, execArgv: [] }, false],
    [{ script: '', execArgv: ['--input-type=module'] }, true],
    [{ script: undefined, execArgv: ['--input-type', 'module'] }, true],
    [{ script: undefined, execArgv: ['--input-type=commonjs'] }, false],
    [{ script: '/app/main.mjs', execArgv: [] }, true],
    [{ script: '/app/main.mts', execArgv: [] }, true],
    [{ script: path.join(fixtures, 'esm-pkg', 'main.cjs'), execArgv: [] }, false],
    [{ script: path.join(fixtures, 'esm-pkg', 'main.cts'), execArgv: ['--experimental-default-type=module'] }, false],
    [{ script: path.join(fixtures, 'esm-pkg', 'main.js'), execArgv: ['--experimental-default-type=commonjs'] }, true],
    [{ script: path.join(fixtures, 'esm-pkg', 'main.js'), execArgv: [] }, true],
    [{ script: path.join(fixtures, 'esm-pkg', 'bin', 'server'), execArgv: [] }, true],
    [{ script: path.join(fixtures, 'bad-pkg', 'main.js'), execArgv: [] }, false],
    [{ script: path.join(__dirname, 'main.js'), execArgv: [] }, false],
    // An explicit package "type" wins over --experimental-default-type (B9 fix round).
    [{ script: path.join(__dirname, 'main.js'), execArgv: ['--experimental-default-type', 'module'] }, false],
    [{ script: path.join(fixtures, 'untyped-pkg', 'main.js'), execArgv: [] }, false],
    [{ script: path.join(fixtures, 'untyped-pkg', 'main.js'), execArgv: ['--experimental-default-type', 'module'] }, true],
    [{ script: path.join(fixtures, 'untyped-pkg', 'main.js'), execArgv: ['--experimental-default-type=module'] }, true],
    [{ script: path.join(path.parse(__dirname).root, 'no-such-dir-b9', 'main.js'), execArgv: [] }, false],
  ];
  for (const [facts, expected] of cases) assert.equal(entryIsEsm(facts), expected, JSON.stringify(facts));
  assert.equal(typeof entryIsEsm(), 'boolean');
});

test('hooksMode() reads the installed mode; undefined without hooks or in an inactive copy', () => {
  const res = runNode(['-e', `
    const { hooksMode } = require('./dist/internal/hooks.js');
    console.log(JSON.stringify({ mode: hooksMode() ?? null }));
  `]);
  assert.deepEqual(lastJson(res), { mode: null });
  const inactive = runNode(['-e', `
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'), { value: { version: '0.0.0-other', hooks: 'sync' } });
    const { hooksMode } = require('./dist/internal/hooks.js');
    console.log(JSON.stringify({ mode: hooksMode() ?? null }));
  `]);
  assert.deepEqual(lastJson(inactive), { mode: null });
});

test('M2 /register in a worker thread: hooks only, silently, under --require; --import does not run there', () => {
  const main = 'test/fixtures/worker-register.cjs';
  const required = runNode(['--require', '@staffops/otel-helper/register', main], { env: QUIET_SETUP });
  assert.equal(required.status, 0, required.stderr);
  assert.deepEqual(lastJson(required), { main: true, worker: { state: true, active: false } });
  assert.equal(withoutDep0205(required.stderr), '');
  // Node runs --import preloads in the main thread only (D-JS14, 2026-11-02).
  const imported = runNode(['--import', '@staffops/otel-helper/register', main], { env: QUIET_SETUP });
  assert.deepEqual(lastJson(imported), { main: true, worker: { state: false, active: false } });
  assert.equal(withoutDep0205(imported.stderr), '');
  // The thread check itself.
  const direct = runNode(['-e', `
    const { runRegister } = require('./dist/internal/register.js');
    console.log(JSON.stringify({ ran: runRegister(false, false), active: !!globalThis[Symbol.for('staffops.otel-helper.state.v1')].runtime?.active }));
  `], { env: QUIET_SETUP });
  assert.deepEqual(lastJson(direct), { ran: true, active: false });
});

test('L code options after a /register setup are warned, not just noted', () => {
  const res = runNode(['--require', '@staffops/otel-helper/register', '-e', `
    require('@staffops/otel-helper').setup({ serviceName: 'ignored' });
  `], { env: QUIET_SETUP });
  const lines = withoutDep0205(res.stderr).split('\n').filter((l) => l && !l.includes('ESM import hooks not installed under --eval'));
  assert.deepEqual(lines, ['otelhelper: setup() already ran from @staffops/otel-helper/register, so these options are ignored; to pass options in code, preload your own file (import @staffops/otel-helper/hooks, then setup({...})) instead of /register']);
});
