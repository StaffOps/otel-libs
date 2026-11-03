'use strict';
// The internal entry @staffops/otel-helper/internal/contrib (task B10c,
// 2026-11-03; extracted from @staffops/otel-helper-sql and -redis, code-review
// M4 of the B10b fix round): the per-process contrib-instance registry the
// opt-in -sql, -redis and -aws packages share — shared children, counted
// enables, tracer/meter binding, the re-require of preloaded files, the
// one-time reports — and the D14 library detection and load errors. Here with
// fake instrumentations; the packages' suites run it against real libraries.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const api = require('@opentelemetry/api');
const contrib = require('@staffops/otel-helper/internal/contrib');

test('internal/contrib exports exactly what the opt-in packages use', () => {
  assert.deepEqual(Object.keys(contrib).filter((k) => k !== '__esModule').sort(), ['appDirectories', 'contribLoadError', 'contribRegistry', 'resolvesFromApp']);
});

/** A fake contrib instrumentation recording what the registry does to it. */
function fakeChild(name) {
  const calls = [];
  let enabled = true; // InstrumentationBase enables itself in its constructor
  return {
    calls,
    instrumentationName: name,
    instrumentationVersion: '1',
    enable: () => ((enabled = true), calls.push('enable')),
    disable: () => ((enabled = false), calls.push('disable')),
    isEnabled: () => enabled,
    setTracerProvider: (p) => calls.push(['tracer', p]),
    setMeterProvider: (p) => calls.push(['meter', p]),
    setLoggerProvider: (p) => calls.push(['logger', p]),
    setConfig: () => undefined,
    getConfig: () => ({}),
  };
}

let diagLines;
function captureDiag() {
  diagLines = [];
  const logger = {
    // Only the registry's own lines (the API logs its global registration at debug).
    warn: (m) => m.startsWith('@staffops/') && diagLines.push(`warn ${m}`),
    debug: (m) => m.startsWith('@staffops/') && diagLines.push(`debug ${m}`),
    info: () => undefined,
    error: () => undefined,
    verbose: () => undefined,
  };
  api.diag.setLogger(logger, { logLevel: api.DiagLogLevel.ALL, suppressOverrideMessage: true });
}
test.afterEach(() => api.diag.disable());

/** A registry over libraries `a` and `b` with fake children; `failing` libraries throw on build. */
function registryOf({ failing = [], packages = { a: ['node:fs'], b: ['node:path'] }, patchedFiles = { a: [], b: [] } } = {}) {
  const children = {};
  const built = [];
  const binds = [];
  const registry = contrib.contribRegistry({
    name: '@staffops/otel-helper-test',
    version: '9.9.9',
    libraries: ['a', 'b'],
    contrib: { a: 'contrib-a', b: 'contrib-b' },
    packages,
    build: (library) => {
      built.push(library);
      if (failing.includes(library)) throw library === 'b' && failing.includes('string') ? 'b thrown string' : new Error(`${library} boom`);
      return (children[library] = fakeChild(`contrib-${library}`));
    },
    patchedFiles,
    tracerProvider: (library, provider) => ({ wrapped: provider, library }),
    meterProvider: (library, provider, current) => (binds.push({ library, current }), { wrapped: provider, library }),
  });
  return { registry, children, built, binds };
}

test('children: built once per library and disabled until enabled; the result covers the contrib names', () => {
  const { registry, children, built } = registryOf();
  const first = registry.instrument({});
  const second = registry.instrument({ a: true });
  assert.deepEqual(built, ['a', 'b'], 'one build per library, shared by every result');
  assert.equal(registry.children(first)[0], children.a);
  assert.deepEqual(children.a.calls, ['disable']);
  assert.equal(first.instrumentationName, '@staffops/otel-helper-test');
  assert.equal(first.instrumentationVersion, '9.9.9');
  assert.deepEqual([...first.libraries], ['a', 'b']);
  assert.deepEqual([...first.coveredInstrumentations], ['contrib-a', 'contrib-b']);
  assert.ok(Object.isFrozen(first.libraries) && Object.isFrozen(first.coveredInstrumentations));
  assert.deepEqual(registry.children(first), [children.a, children.b]);
  assert.deepEqual(registry.children(second), [children.a, children.b]);
  assert.deepEqual([...registry.instrument({ b: false }).libraries], ['a']);
  assert.deepEqual(registry.children({}), [], 'anything else has no children');
  assert.deepEqual(registryOf().registry.children(first), [], 'nor a result of another registry');
  assert.equal('children' in first, false, 'the children are not a public member');
});

test('enable/disable are counted per child across results; the last disable turns a child off', () => {
  captureDiag();
  const { registry, children } = registryOf();
  const one = registry.instrument({});
  const two = registry.instrument({ b: false });
  one.enable();
  one.enable(); // counted once per result
  two.enable();
  assert.deepEqual(one.getConfig(), { enabled: true });
  assert.ok(children.a.isEnabled() && children.b.isEnabled());
  one.disable();
  assert.equal(children.a.isEnabled(), true, 'still enabled by the other result');
  assert.equal(children.b.isEnabled(), false);
  one.disable(); // idempotent
  two.disable();
  assert.equal(children.a.isEnabled(), false);
  assert.deepEqual(children.a.calls.filter((c) => c === 'enable'), ['enable'], 'enabled once while counted');
  assert.deepEqual(two.getConfig(), { enabled: false });
});

test('setConfig() is ignored and warned once per registry, on the next enable (or at once when enabled)', () => {
  captureDiag();
  const { registry } = registryOf();
  const r = registry.instrument({});
  r.setConfig({ enabled: true });
  assert.deepEqual(r.getConfig(), { enabled: false });
  assert.deepEqual(diagLines, [], 'buffered until enable (the DiagLogger may not exist yet)');
  r.enable();
  r.setConfig({});
  r.disable();
  r.enable();
  assert.deepEqual(diagLines.filter((l) => l.includes('setConfig() is ignored')), [
    'warn @staffops/otel-helper-test: setConfig() is ignored; the configuration is fixed and enabled follows setup()/shutdown()',
  ]);
  const other = registryOf().registry.instrument({});
  other.enable();
  other.setConfig({});
  assert.equal(diagLines.filter((l) => l.includes('setConfig() is ignored')).length, 2, 'once per registry, immediately when enabled');
});

test('the same TracerProvider is not re-bound; every meter bind is a new generation, restored when it throws', () => {
  const { registry, children, binds } = registryOf();
  const r = registry.instrument({});
  const tp = { id: 'tp' };
  r.setTracerProvider(tp);
  r.setTracerProvider(tp);
  assert.deepEqual(children.a.calls.filter((c) => c[0] === 'tracer'), [['tracer', { wrapped: tp, library: 'a' }]]);
  r.setTracerProvider({ id: 'tp2' });
  assert.equal(children.b.calls.filter((c) => c[0] === 'tracer').length, 2);
  const mp = { id: 'mp' };
  r.setMeterProvider(mp);
  const firstA = binds.find((b) => b.library === 'a');
  assert.equal(firstA.current(), true);
  r.setMeterProvider(mp);
  assert.equal(firstA.current(), false, 'an older bind records nothing');
  const latestA = binds.filter((b) => b.library === 'a').pop();
  assert.equal(latestA.current(), true);
  children.a.setMeterProvider = () => {
    throw new Error('bind boom');
  };
  assert.throws(() => r.setMeterProvider(mp), /bind boom/);
  assert.equal(latestA.current(), true, 'a failing bind restores the previous generation');
  // A first bind that throws leaves no generation behind.
  const fresh = registryOf();
  const f = fresh.registry.instrument({ b: false });
  fresh.children.a.setMeterProvider = () => {
    throw new Error('first boom');
  };
  assert.throws(() => f.setMeterProvider(mp), /first boom/);
  assert.equal(fresh.binds[0].current(), false);
  r.setLoggerProvider('lp');
  assert.deepEqual(children.a.calls.at(-1), ['logger', 'lp']);
  delete children.b.setLoggerProvider;
  r.setLoggerProvider('lp2'); // optional on a child
});

test('instrument(): a failed build throws when named true, else is skipped and reported; none loaded -> the first error', () => {
  captureDiag();
  assert.throws(() => registryOf({ failing: ['b'] }).registry.instrument({ b: true }), { message: 'b boom' });
  assert.throws(() => registryOf({ failing: ['a', 'b'] }).registry.instrument({}), { message: 'a boom' });
  assert.throws(() => registryOf({ failing: ['a'] }).registry.instrument({ b: false }), { message: 'a boom' });
  const { registry } = registryOf({ failing: ['a'] });
  const r = registry.instrument({});
  assert.deepEqual([...r.libraries], ['b']);
  r.enable();
  r.disable();
  r.enable();
  assert.deepEqual(diagLines, ["warn @staffops/otel-helper-test: 'a' is not instrumented: a boom"], 'reported once, on the first enable');
  const thrown = registryOf({ failing: ['b', 'string'] });
  thrown.registry.instrument({ a: true, b: undefined }).enable();
  assert.ok(diagLines.some((l) => l.endsWith("'b' is not instrumented: b thrown string")), 'a non-Error throw is reported as a string');
});

test('instrument(): an unresolved library is instrumented and reported once: WARN when named true, debug in default mode', () => {
  captureDiag();
  const packages = { a: ['no-such-package-otel-contrib-test'], b: ['node:path'] };
  const quiet = registryOf({ packages }).registry.instrument({});
  quiet.enable();
  assert.deepEqual([...quiet.libraries], ['a', 'b']);
  assert.equal(diagLines.length, 1, diagLines.join('\n'));
  assert.match(diagLines[0], /^debug @staffops\/otel-helper-test: 'a' could not be resolved from .+; instrumented if it loads$/);
  diagLines = [];
  const named = registryOf({ packages }).registry.instrument({ a: true });
  named.enable();
  assert.deepEqual(diagLines.map((l) => l.split(' ', 1)[0]), ['warn', 'debug']);
  assert.ok(diagLines[0].includes(process.cwd()) && !diagLines[0].includes('node_modules'), `the WARN names the entry directory and cwd only: ${diagLines[0]}`);
});

test('the first enable re-requires the cached patched files of each library (every copy; a main entry only when cached)', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'otel-contrib-'));
  try {
    const write = (rel, source) => {
      const file = path.join(dir, rel);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, source);
      return file;
    };
    // Two copies of `lib` (one nested), a scoped one, and a copy whose main does not resolve.
    write('node_modules/lib/package.json', '{"name":"lib","main":"main.js"}');
    const main = write('node_modules/lib/main.js', 'globalThis.__contribLoads = (globalThis.__contribLoads || 0) + 1;');
    const inner = write('node_modules/lib/inner/patched.js', 'module.exports = 1;');
    write('node_modules/app/node_modules/lib/package.json', '{"name":"lib","main":"main.js"}');
    const nestedMain = write('node_modules/app/node_modules/lib/main.js', 'module.exports = 2;');
    write('node_modules/@scope/pkg/package.json', '{"name":"@scope/pkg","main":"gone.js"}');
    const scoped = write('node_modules/@scope/pkg/other.js', 'module.exports = 3;');
    for (const f of [main, inner, nestedMain, scoped]) require(f);
    const notCached = write('node_modules/lib/inner/never.js', 'throw new Error("never required");');
    const required = [];
    const original = require('node:module').prototype.require;
    require('node:module').prototype.require = function (id) {
      if (path.isAbsolute(id) && id.startsWith(dir)) required.push(path.relative(dir, id));
      return original.apply(this, arguments);
    };
    try {
      const { registry } = registryOf({
        patchedFiles: {
          a: [['lib', ''], ['lib', path.join('inner', 'patched.js')], ['lib', path.join('inner', 'never.js')]],
          b: [['@scope/pkg', ''], ['@scope/pkg', 'other.js'], ['absent', '']],
        },
      });
      const r = registry.instrument({});
      r.enable();
      r.disable();
      r.enable(); // checked once per library
      registry.instrument({}).enable();
    } finally {
      require('node:module').prototype.require = original;
    }
    assert.deepEqual(required.sort(), [
      path.join('node_modules', '@scope', 'pkg', 'other.js'),
      path.join('node_modules', 'app', 'node_modules', 'lib', 'main.js'),
      path.join('node_modules', 'lib', 'inner', 'patched.js'),
      path.join('node_modules', 'lib', 'main.js'),
    ]);
    assert.ok(fs.existsSync(notCached));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('appDirectories(): entry directory, cwd, then the main module lookup paths, deduplicated', () => {
  assert.deepEqual(contrib.appDirectories('/srv/app/main.js', '/work', []), ['/srv/app', '/work']);
  assert.deepEqual(contrib.appDirectories('main.js', '/work', ['/work/node_modules', '/node_modules']), ['/work', '/work/node_modules', '/node_modules']);
  assert.deepEqual(contrib.appDirectories('', '/work', []), ['/work']);
  assert.ok(contrib.appDirectories().length >= 1, 'defaults read the process');
  // An ES module entry has no require.main: no lookup paths.
  const entry = require.resolve('@staffops/otel-helper/internal/contrib');
  const res = spawnSync(process.execPath, ['--input-type=module', '-e', `import c from ${JSON.stringify(entry)}; console.log(JSON.stringify(c.appDirectories(undefined, '/work')))`], { encoding: 'utf8' });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(JSON.parse(res.stdout), ['/work']);
});

test('resolvesFromApp(): any of the names resolves from the directories, without loading it', () => {
  assert.equal(contrib.resolvesFromApp(['no-such-package-otel-contrib-test', '@opentelemetry/api'], [__dirname]), true);
  assert.equal(contrib.resolvesFromApp(['@opentelemetry/api'], [os.tmpdir()]), false);
  assert.equal(contrib.resolvesFromApp([]), false);
  assert.equal(contrib.resolvesFromApp(['@opentelemetry/api']), true, 'defaults to the app directories');
});

test('contribLoadError(): "not installed" only when the module itself is missing; else the first line verbatim; cause kept', () => {
  const mod = '@opentelemetry/instrumentation-x';
  const missing = Object.assign(new Error(`Cannot find module '${mod}'\nRequire stack:\n- /x/index.js`), { code: 'MODULE_NOT_FOUND' });
  const notInstalled = contrib.contribLoadError('X', '@staffops/otel-helper-x', mod, missing);
  assert.equal(notInstalled.message, `X instrumentation not installed: ${mod} is missing. Reinstall @staffops/otel-helper-x (it depends on ${mod}).`);
  assert.equal(notInstalled.cause, missing);
  const transitive = Object.assign(new Error("Cannot find module 'other'\nRequire stack:\n- /x"), { code: 'MODULE_NOT_FOUND' });
  assert.equal(contrib.contribLoadError('X', 'p', mod, transitive).message, "failed to import the X instrumentation: Cannot find module 'other'");
  assert.equal(contrib.contribLoadError('X', 'p', mod, new TypeError('boom')).message, 'failed to import the X instrumentation: boom');
  assert.equal(contrib.contribLoadError('X', 'p', mod, 'thrown string').message, 'failed to import the X instrumentation: thrown string');
  assert.equal(contrib.contribLoadError('X', 'p', mod, null).message, 'failed to import the X instrumentation: null');
  assert.equal(contrib.contribLoadError('X', 'p', mod, new Error('')).message, 'failed to import the X instrumentation: ');
});
