'use strict';
// @staffops/otel-helper-redis without a real server (task B10b; spec JS8,
// design D-JS9, contract R10, D14 and A25): options, the library detection and
// load errors, the sanitized statement, the lifecycle through setup(), and
// every command form of node-redis 6 and ioredis 6 against the in-process RESP
// fake (test/fixtures/resp-fake.cjs) — no argument, password or connection
// string recorded, ERROR without description, a library loaded before setup(),
// a re-setup, the database semconv mode and an ESM custom preload.
// server.test.cjs runs the same matrix against a real Redis.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { InMemorySpanExporter, SimpleSpanProcessor, TracerProvider } = require('@opentelemetry/sdk-trace');
const { MeterProvider } = require('@opentelemetry/sdk-metrics');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { redisInstrumentation } = require('@staffops/otel-helper-redis');
const libraries = require('../dist/internal/libraries.js');
const statement = require('../dist/internal/statement.js');
const { childrenForTests } = require('../dist/internal/redis-instrumentation.js');
const { startRespFake } = require('./fixtures/resp-fake.cjs');
const { runScenario, assertScenario } = require('./fixtures/scenario.cjs');

const ENTRY = require.resolve('@staffops/otel-helper-redis');
const PKG_DIR = path.resolve(__dirname, '..');
const FIXTURES = path.join(__dirname, 'fixtures');
const PASSWORD = 'fake-pass-only';
const REFUSED = 'redis://127.0.0.1:1';

// One instance for the whole file, created before redis and ioredis load (as an app does).
const instrumentation = redisInstrumentation();

let saved;
let records;
let exporter;
test.beforeEach(() => {
  saved = { ...process.env };
  for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  testing.reset();
  records = [];
  testing.setDiagnosticSink((line) => records.push(line));
  exporter = new InMemorySpanExporter();
});
test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

function setupWith(inst = instrumentation) {
  helper.setup({ instrumentations: [inst], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
}

/** Runs `node <args>` asynchronously (the parent's RESP fake must keep answering); resolves the JSON it prints last, stderr and status. */
function runNode(args, { cwd = PKG_DIR, env = {} } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, {
      cwd,
      env: { ...process.env, OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=DEV', OTEL_HELPER_METRICS_PORT: '0', ...env },
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += d));
    child.stderr.on('data', (d) => (stderr += d));
    const timer = setTimeout(() => child.kill('SIGKILL'), 60_000);
    child.on('close', (status) => {
      clearTimeout(timer);
      const last = stdout.trim().split('\n').pop();
      try {
        resolve({ ...JSON.parse(last), stderr, status });
      } catch {
        reject(new Error(`child status=${status}\nstdout:\n${stdout}\nstderr:\n${stderr}`));
      }
    });
  });
}

/** Runs `source` (CommonJS) as `<dir>/main.cjs` with cwd `dir`. */
function runIn(dir, source, env = {}) {
  const script = path.join(dir, 'main.cjs');
  fs.writeFileSync(script, source);
  return runNode([script], { cwd: dir, env });
}

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'otel-redis-'));
}

// ── options ────────────────────────────────────────────────────────────

test('invalid options throw a TypeError naming the problem (R8)', () => {
  class Opts {}
  for (const [value, message] of [
    [null, /options must be an object, got null/],
    [[], /options must be an object, got array/],
    ['redis', /options must be an object, got string/],
    [new Opts(), /options must be an object, got object/],
    [{ redis: true, pg: true }, /unknown key\(s\) pg/],
    [{ redis: 'yes' }, /redis must be a boolean, got string/],
    [{ ioredis: 1 }, /ioredis must be a boolean, got number/],
    [{ redis: false, ioredis: false }, /both false; nothing to instrument/],
  ]) {
    assert.throws(() => redisInstrumentation(value), { name: 'TypeError', message }, JSON.stringify(value));
  }
});

test('undefined option values count as not set; a null-prototype object is accepted', () => {
  const opts = Object.create(null);
  opts.redis = undefined;
  opts.other = undefined;
  assert.deepEqual([...redisInstrumentation(opts).libraries], ['redis', 'ioredis']);
  assert.deepEqual([...redisInstrumentation({ ioredis: false }).libraries], ['redis']);
  assert.deepEqual([...redisInstrumentation({ redis: false, ioredis: true }).libraries], ['ioredis']);
});

test('the result is one Instrumentation named after the package, covering the contrib names (M2)', () => {
  assert.equal(instrumentation.instrumentationName, '@staffops/otel-helper-redis');
  assert.equal(instrumentation.instrumentationVersion, require('../package.json').version);
  assert.deepEqual([...instrumentation.coveredInstrumentations], ['@opentelemetry/instrumentation-redis', '@opentelemetry/instrumentation-ioredis']);
  assert.deepEqual([...redisInstrumentation({ redis: false }).coveredInstrumentations], ['@opentelemetry/instrumentation-ioredis']);
  for (const child of childrenForTests(instrumentation)) assert.ok(instrumentation.coveredInstrumentations.includes(child.instrumentationName), child.instrumentationName);
  assert.deepEqual(instrumentation.getConfig(), { enabled: false });
});

test('setConfig is ignored (enabled is the real state) and warned once per process, on the next enable', async () => {
  instrumentation.setConfig({ enabled: true }); // before setup(): no DiagLogger yet, buffered
  assert.deepEqual(instrumentation.getConfig(), { enabled: false });
  setupWith();
  instrumentation.setConfig({ enabled: false });
  assert.deepEqual(instrumentation.getConfig(), { enabled: true });
  assert.equal(records.filter((l) => l.includes('setConfig() is ignored')).length, 1, records.join('\n'));
});

test('M2: a raw contrib instrumentation next to redisInstrumentation() is refused by setup()', () => {
  const { IORedisInstrumentation } = require('@opentelemetry/instrumentation-ioredis');
  const raw = new IORedisInstrumentation({ enabled: false });
  assert.throws(() => helper.setup({ instrumentations: [instrumentation, raw] }), {
    name: 'TypeError',
    message: /two instrumentations for "@opentelemetry\/instrumentation-ioredis"/,
  });
  assert.throws(() => helper.setup({ instrumentations: [instrumentation, redisInstrumentation({ ioredis: false })] }), {
    message: /two instrumentations for "@staffops\/otel-helper-redis"/,
  });
});

// ── statement ─────────────────────────────────────────────────────────

test('Q3 (2026-11-03): span names are the command uppercased; connection spans are connect', () => {
  assert.equal(statement.commandSpanName('redis-SET'), 'SET');
  assert.equal(statement.commandSpanName('redis-connect'), 'connect');
  assert.equal(statement.commandSpanName('connect'), 'connect');
  assert.equal(statement.commandSpanName('set'), 'SET');
  assert.equal(statement.commandSpanName('multi'), 'MULTI');
  assert.equal(statement.commandSpanName('redis-NoSuchCmd'), 'NOSUCHCMD');
  assert.equal(statement.commandSpanName('redis-'), 'redis', 'an empty name falls back to db.system.name');
  assert.equal(statement.commandSpanName(''), 'redis');
});

test('db.query.text: the command name uppercased and one ? per argument, never a value (Python format)', () => {
  assert.equal(statement.sanitizedStatement('set', ['k', 'v']), 'SET ? ?');
  assert.equal(statement.sanitizedStatement('GET', ['k']), 'GET ?');
  assert.equal(statement.sanitizedStatement('ping', []), 'PING');
  assert.equal(statement.sanitizedStatement('Auth', ['user', 'pass']), 'AUTH ? ?');
  assert.equal(statement.sanitizedStatement('eval', ['return 1', '1', 'k', 'v']), 'EVAL ? ? ? ?');
  assert.equal(statement.sanitizedStatement(Buffer.from('hset'), [Buffer.from('k'), 'f', 'v']), 'HSET ? ? ?');
  assert.equal(statement.sanitizedStatement('quit', undefined), 'QUIT');
  const long = statement.sanitizedStatement('MSET', new Array(600).fill('v'));
  assert.equal(long.length, statement.MAX_STATEMENT);
  assert.ok(long.endsWith('...') && long.startsWith('MSET ? ?'));
  // Fix round nits (2026-11-03): empty name, any typed array, a capped count, code points.
  assert.equal(statement.sanitizedStatement('', ['k']), 'redis ?');
  assert.equal(statement.sanitizedStatement(new Uint8Array([103, 101, 116]), ['k']), 'GET ?');
  assert.equal(statement.sanitizedStatement('mset', { length: 10 ** 9 }), 'MSET', 'not an array: no arguments counted');
  assert.equal(Array.from(statement.sanitizedStatement('MSET', new Array(10 ** 6).fill(0))).length, statement.MAX_STATEMENT);
  const wide = statement.sanitizedStatement('😀'.repeat(1200), []);
  assert.equal(Array.from(wide).length, statement.MAX_STATEMENT);
  assert.ok(!/[\ud800-\udbff]\.\.\.$/.test(wide), 'never cut inside a surrogate pair');
});

test('A26: the reply code is a known leading code (allowlist: WRONGTYPE, ERR, NOAUTH, MOVED, ...); else none', () => {
  assert.ok(statement.REPLY_CODES.has('WRONGPASS') && !statement.REPLY_CODES.has('NOTREADY'));
  for (const [message, want] of [
    ['WRONGTYPE Operation against a key holding the wrong kind of value', 'WRONGTYPE'],
    ["ERR unknown command 'X'", 'ERR'],
    ['NOAUTH Authentication required.', 'NOAUTH'],
    ['MY_CODE2 from a script', undefined],
    ['SECRET_TOKEN42 bad', undefined],
    ['MOVED 3999 127.0.0.1:6381', 'MOVED'],
    ['NOSCRIPT No matching script', 'NOSCRIPT'],
    ['NOTREADY x', undefined],
    ['connect ECONNREFUSED 127.0.0.1:1', undefined],
    ['Connection is closed.', undefined],
    ['NOSPACE', undefined],
    [`${'A'.repeat(33)} too long`, undefined],
  ]) {
    assert.equal(statement.redisResponseCode(message), want, message);
  }
});

// ── library detection and D14 load errors (as -sql R3) ────────────────

test('R3: unresolved libraries are still instrumented; WARN only when named true, diag debug in default mode', async () => {
  const dir = tempDir();
  try {
    const src = (opts) => `
      const helper = require(${JSON.stringify(require.resolve('@staffops/otel-helper'))});
      const inst = require(${JSON.stringify(ENTRY)}).redisInstrumentation(${opts});
      helper.setup({ instrumentations: [inst] });
      console.log(JSON.stringify({ libraries: inst.libraries }));`;
    const lines = (out, lib) => out.stderr.split('\n').filter((l) => l.includes(`'${lib}' could not be resolved from`));
    const quiet = await runIn(dir, src(''));
    assert.deepEqual(quiet.libraries, ['redis', 'ioredis']);
    assert.deepEqual([...lines(quiet, 'redis'), ...lines(quiet, 'ioredis')], [], 'default mode: no WARN for an absent library');
    const debug = await runIn(dir, src(''), { OTEL_LOG_LEVEL: 'DEBUG' });
    for (const lib of ['redis', 'ioredis']) assert.equal(lines(debug, lib).length, 1, `debug line for ${lib}: ${debug.stderr}`);
    const named = await runIn(dir, src('{ redis: true, ioredis: true }'));
    assert.deepEqual(named.libraries, ['redis', 'ioredis'], 'named true: never refused because of resolution');
    for (const lib of ['redis', 'ioredis']) {
      const got = lines(named, lib);
      assert.equal(got.length, 1, named.stderr);
      assert.ok(got[0].includes(dir) && got[0].includes('; instrumented if it loads'), got[0]);
      assert.ok(!got[0].includes('node_modules'), `the WARN names the entry directory and cwd only: ${got[0]}`);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/** Child source that makes the contrib modules in `poisoned` throw on load (a require.cache entry with a throwing getter). */
function poison(poisoned) {
  return poisoned
    .map((mod) => `{ const f = require.resolve(${JSON.stringify(mod)}, { paths: [${JSON.stringify(PKG_DIR)}] });
      const m = new (require('node:module'))(f); m.loaded = true; m.filename = f;
      Object.defineProperty(m, 'exports', { get() { throw new Error('contrib boom'); } });
      require.cache[f] = m; }`)
    .join('\n');
}

test('D14: no contrib instrumentation loads -> the first load error is thrown; named true -> its own; else reported', async () => {
  const dir = tempDir();
  try {
    const both = await runIn(dir, `${poison(['@opentelemetry/instrumentation-redis', '@opentelemetry/instrumentation-ioredis'])}
      try { require(${JSON.stringify(ENTRY)}).redisInstrumentation(); console.log('{}'); }
      catch (e) { console.log(JSON.stringify({ message: e.message, cause: e.cause && e.cause.message })); }`);
    assert.deepEqual({ message: both.message, cause: both.cause }, { message: 'failed to import the Redis instrumentation: contrib boom', cause: 'contrib boom' });
    const named = await runIn(dir, `${poison(['@opentelemetry/instrumentation-ioredis'])}
      try { require(${JSON.stringify(ENTRY)}).redisInstrumentation({ ioredis: true }); console.log('{}'); }
      catch (e) { console.log(JSON.stringify({ message: e.message })); }`);
    assert.equal(named.message, 'failed to import the Redis instrumentation: contrib boom');
    const one = await runIn(dir, `${poison(['@opentelemetry/instrumentation-redis'])}
      const helper = require(${JSON.stringify(require.resolve('@staffops/otel-helper'))});
      const inst = require(${JSON.stringify(ENTRY)}).redisInstrumentation();
      helper.setup({ instrumentations: [inst] });
      console.log(JSON.stringify({ libraries: inst.libraries }));`);
    assert.deepEqual(one.libraries, ['ioredis']);
    assert.match(one.stderr, /'redis' is not instrumented: failed to import the Redis instrumentation: contrib boom/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('D14: a contrib module that is missing is "not installed"; any other load error is reported verbatim', () => {
  const mod = '@opentelemetry/instrumentation-ioredis';
  const missing = Object.assign(new Error(`Cannot find module '${mod}'\nRequire stack:\n- /x/index.js`), { code: 'MODULE_NOT_FOUND' });
  const notInstalled = libraries.instrumentationLoadError(mod, missing);
  assert.equal(notInstalled.message, `Redis instrumentation not installed: ${mod} is missing. Reinstall @staffops/otel-helper-redis (it depends on ${mod}).`);
  assert.equal(notInstalled.cause, missing);
  const transitive = Object.assign(new Error("Cannot find module '@opentelemetry/redis-common'\nRequire stack:\n- /x"), { code: 'MODULE_NOT_FOUND' });
  assert.equal(libraries.instrumentationLoadError(mod, transitive).message, "failed to import the Redis instrumentation: Cannot find module '@opentelemetry/redis-common'");
  assert.equal(libraries.instrumentationLoadError(mod, new TypeError('boom')).message, 'failed to import the Redis instrumentation: boom');
  assert.equal(libraries.instrumentationLoadError(mod, 'thrown string').message, 'failed to import the Redis instrumentation: thrown string');
});

test('the libraries are resolved from the app without being loaded; redis counts via redis or @redis/client', () => {
  const loaded = () => Object.keys(require.cache).filter((f) => /[\\/]node_modules[\\/](redis|@redis[\\/]client|ioredis)[\\/]/.test(f));
  assert.deepEqual(loaded(), [], 'nothing above loads redis or ioredis');
  redisInstrumentation();
  assert.deepEqual(loaded(), []);
  assert.deepEqual(libraries.PACKAGES.redis, ['redis', '@redis/client']);
  assert.equal(libraries.resolvesFromApp('redis', [__dirname]), true);
  assert.equal(libraries.resolvesFromApp('ioredis', [__dirname]), true);
  assert.equal(libraries.resolvesFromApp('ioredis', [os.tmpdir()]), false);
  assert.deepEqual(libraries.appDirectories('/srv/app/main.js', '/work', []), ['/srv/app', '/work']);
  assert.deepEqual(libraries.appDirectories('', '/work', ['/work/node_modules']), ['/work', '/work/node_modules']);
  assert.ok(libraries.appDirectories().length >= 1, 'defaults read the process');
});

// ── lifecycle ─────────────────────────────────────────────────────────

test('setup() binds and enables it, shutdown() disables it; every result shares the contrib instrumentations', async () => {
  const children = childrenForTests(instrumentation);
  assert.equal(children.length, 2);
  assert.deepEqual(childrenForTests({}), [], 'only results of redisInstrumentation()');
  assert.ok(children.every((c) => !c.isEnabled()), 'disabled until setup()');
  setupWith();
  assert.equal(instrumentation.getConfig().enabled, true);
  assert.ok(children.every((c) => c.isEnabled()));
  const other = redisInstrumentation({ redis: false });
  assert.equal(childrenForTests(other)[0], children[1], 'shared ioredis child');
  other.enable();
  other.enable(); // counted once per result
  await helper.shutdown();
  assert.equal(instrumentation.getConfig().enabled, false);
  assert.equal(children[1].isEnabled(), true, 'still enabled by the other result');
  other.disable();
  other.disable(); // idempotent
  assert.ok(children.every((c) => !c.isEnabled()));
});

test('the same TracerProvider again is not re-bound; a meter bind always is; a failing meter bind restores the generation', () => {
  const inst = redisInstrumentation({ redis: false });
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
    child.setMeterProvider = () => {
      throw new Error('bind boom');
    };
    assert.throws(() => inst.setMeterProvider(mp), /bind boom/);
    const fresh = redisInstrumentation({ ioredis: false });
    const [redisChild] = childrenForTests(fresh);
    const keep = redisChild.setMeterProvider;
    redisChild.setMeterProvider = () => {
      throw new Error('first bind boom');
    };
    try {
      assert.throws(() => fresh.setMeterProvider(mp), /first bind boom/);
    } finally {
      redisChild.setMeterProvider = keep;
    }
  } finally {
    Object.assign(child, { setTracerProvider: original.t, setMeterProvider: original.m, setLoggerProvider: original.l });
  }
  assert.deepEqual(calls.map(([k]) => k), ['t', 'm', 'm', 'l']);
});

test('OTEL_SDK_DISABLED=true leaves it disabled (no instrumentation, as Python)', () => {
  process.env.OTEL_SDK_DISABLED = 'true';
  setupWith();
  assert.equal(instrumentation.getConfig().enabled, false);
  assert.ok(childrenForTests(instrumentation).every((c) => !c.isEnabled()));
});

// ── against the RESP fake ─────────────────────────────────────────────

test('every command form of node-redis and ioredis: sanitized statements, no secrets, ERROR without description', async () => {
  const fake = await startRespFake({ password: PASSWORD });
  try {
    setupWith();
    await runScenario({ url: fake.url, refusedUrl: REFUSED });
    assertScenario(exporter.getFinishedSpans(), { url: fake.url, refusedUrl: REFUSED });
    // The fake saw the real arguments: the client sent them, the spans did not record them.
    assert.ok(fake.commands.some((c) => c[0] === 'SET' && c[2] === 'secret-value'));
    assert.ok(fake.commands.some((c) => c[0].toUpperCase() === 'AUTH' && c.includes(PASSWORD)));
  } finally {
    await fake.close();
  }
});

test('re-setup: shutdown() unpatches (no spans), the next setup() instruments again to the new providers', async () => {
  const fake = await startRespFake();
  const { createClient } = require('redis');
  const IORedis = require('ioredis');
  try {
    setupWith();
    const c = createClient({ url: fake.url });
    c.on('error', () => undefined);
    await c.connect();
    const r = new IORedis(fake.url, { lazyConnect: true });
    await r.connect();
    await helper.shutdown();
    await c.get('a');
    await r.get('a');
    assert.equal(exporter.getFinishedSpans().filter((s) => s.name === 'GET').length, 0, 'disabled after shutdown');
    const next = new InMemorySpanExporter();
    helper.setup({ instrumentations: [instrumentation], spanProcessors: [new SimpleSpanProcessor({ exporter: next })] });
    await c.get('b');
    await r.get('b');
    assert.deepEqual(next.getFinishedSpans().map((s) => [s.name, s.instrumentationScope.name]).sort(), [
      ['GET', '@opentelemetry/instrumentation-ioredis'],
      ['GET', '@opentelemetry/instrumentation-redis'],
    ], 'clients built earlier report again');
    await c.quit();
    await r.quit();
  } finally {
    await fake.close();
  }
});

test('redis and ioredis loaded (and clients built) before redisInstrumentation() and setup() are still instrumented', async () => {
  const fake = await startRespFake();
  try {
    const out = await runNode([path.join(FIXTURES, 'preload.cjs')], { env: { REDIS_FAKE_URL: fake.url } });
    assert.equal(out.status, 0, out.stderr);
    assert.ok(out.preloaded, 'redis and ioredis were in require.cache first');
    assert.deepEqual(out.spans.filter((s) => s.name === 'SET').map((s) => [s.scope, s.attributes['db.query.text'], s.attributes['db.system']]).sort(), [
      ['@opentelemetry/instrumentation-ioredis', 'SET ? ?', 'redis'],
      ['@opentelemetry/instrumentation-redis', 'SET ? ?', 'redis'],
    ]);
  } finally {
    await fake.close();
  }
});

test('OTEL_SEMCONV_STABILITY_OPT_IN=database: only db.system.name', async () => {
  const fake = await startRespFake();
  try {
    const out = await runNode([path.join(FIXTURES, 'preload.cjs')], { env: { REDIS_FAKE_URL: fake.url, FIXTURE_NO_PRELOAD: '1', OTEL_SEMCONV_STABILITY_OPT_IN: 'http,database' } });
    assert.equal(out.status, 0, out.stderr);
    assert.equal(out.preloaded, false);
    assert.ok(out.spans.length >= 2);
    for (const s of out.spans) {
      assert.equal(s.attributes['db.system.name'], 'redis', s.name);
      assert.equal(s.attributes['db.system'], undefined, s.name);
    }
  } finally {
    await fake.close();
  }
});

test('D-JS11: an ESM app with a custom preload (hooks + setup({ instrumentations })) gets redis and ioredis spans; without the hooks only redis', async () => {
  const fake = await startRespFake();
  try {
    for (const noHooks of ['', '1']) {
      const out = await runNode(['--import', path.join(FIXTURES, 'esm-preload.mjs'), path.join(FIXTURES, 'esm-app.mjs')], { env: { REDIS_FAKE_URL: fake.url, FIXTURE_NO_HOOKS: noHooks } });
      assert.equal(out.status, 0, out.stderr);
      if (noHooks === '') {
        assert.ok(out.hooks === 'sync' || out.hooks === 'async', `hooks mode ${out.hooks}`);
        assert.doesNotMatch(out.stderr, /without the module hooks/);
      } else {
        assert.equal(out.hooks, null);
        assert.match(out.stderr, /without the module hooks; packages imported after setup\(\) \(express, @grpc\/grpc-js, ioredis\)/);
      }
      // Control (hooks off): node-redis is still instrumented — the patched file,
      // @redis/client's client/index.js, is loaded by require() inside the
      // package — but ioredis is not: its patched file is the package entry,
      // which an ES module `import` loads without require() (the JS5 warning).
      const want = noHooks === '' ? ['@opentelemetry/instrumentation-ioredis', '@opentelemetry/instrumentation-redis'] : ['@opentelemetry/instrumentation-redis'];
      assert.deepEqual(out.spans.filter((s) => s.name === 'SET' && s.attributes['db.query.text'] === 'SET ? ?').map((s) => s.scope).sort(), want, `hooks=${noHooks === '' ? 'on' : 'off'}`);
    }
  } finally {
    await fake.close();
  }
});
