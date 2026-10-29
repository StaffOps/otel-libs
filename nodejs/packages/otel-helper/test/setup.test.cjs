'use strict';
// setup()/shutdown() for task B3: identity resolution (spec JS4, contract R4/R5,
// D1, D4, ADR-003), deprecations (D8, Go G9), diagnostics and the DiagLogger
// (spec JS3, design D-JS5), minimal lifecycle (the rest of the lifecycle
// requirement is task B9, which cites it).
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('@opentelemetry/api');
const { TracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { runCjs, lastJson } = require('./helpers.cjs');

const IDENTITY_VARS = [
  'OTEL_SERVICE_NAME', 'OTEL_RESOURCE_ATTRIBUTES', 'SERVICE_NAME', 'ENVIRONMENT', 'OTEL_HELPER_DEBUG_LEVEL',
  'OTEL_HELPER_SAMPLE_RATIO', 'OTEL_HELPER_DISABLED_SIGNALS', 'OTEL_HELPER_EXTRA_INSTRUMENTATION', 'OTEL_LOG_LEVEL',
];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

let records;
let saved;

// reset() also restores the stderr sink: capture again after every reset.
function resetAndCapture() {
  testing.reset();
  records = [];
  testing.setDiagnosticSink((line, meta) => records.push({ line, ...meta }));
}

test.beforeEach(() => {
  saved = Object.fromEntries(IDENTITY_VARS.map((k) => [k, process.env[k]]));
  for (const k of IDENTITY_VARS) delete process.env[k];
  resetAndCapture();
});

test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

const lines = (filter = () => true) => records.filter(filter).map((r) => r.line);
const attrs = () => ({ ...testing.getResource().attributes });

function setEnv(vars) {
  for (const [k, v] of Object.entries(vars)) process.env[k] = v;
}

test('JS4 precedence of service.name: code > OTEL_SERVICE_NAME > OTEL_RESOURCE_ATTRIBUTES > SERVICE_NAME', () => {
  const cases = [
    [{ SERVICE_NAME: 'legacy' }, {}, 'legacy'],
    [{ SERVICE_NAME: 'legacy', OTEL_RESOURCE_ATTRIBUTES: 'service.name=ra' }, {}, 'ra'],
    [{ OTEL_SERVICE_NAME: 'std', OTEL_RESOURCE_ATTRIBUTES: 'service.name=ra', SERVICE_NAME: 'legacy' }, {}, 'std'],
    [{ OTEL_SERVICE_NAME: 'std' }, { resourceAttributes: { 'service.name': 'code-ra' } }, 'code-ra'],
    [{ OTEL_SERVICE_NAME: 'std' }, { serviceName: ' code ', resourceAttributes: { 'service.name': 'code-ra' } }, 'code'],
    [{ OTEL_SERVICE_NAME: ' \t', OTEL_RESOURCE_ATTRIBUTES: 'service.name=ra' }, {}, 'ra'],
    [{ OTEL_RESOURCE_ATTRIBUTES: 'service.name= ', SERVICE_NAME: ' legacy\n' }, {}, 'legacy'],
  ];
  for (const [env, options, want] of cases) {
    resetAndCapture();
    for (const k of IDENTITY_VARS) delete process.env[k];
    setEnv(env);
    const resolved = helper.setup(options);
    assert.equal(resolved.serviceName, want, JSON.stringify(env));
    assert.equal(attrs()['service.name'], want, JSON.stringify(env));
  }
});

test('R5 no service name: the SDK default unknown_service:<argv0> and one warning naming OTEL_SERVICE_NAME', () => {
  const resolved = helper.setup();
  assert.match(resolved.serviceName, /^unknown_service(:.+)?$/);
  assert.equal(attrs()['service.name'], resolved.serviceName);
  assert.deepEqual(lines((r) => r.line.includes('OTEL_SERVICE_NAME')), [`otelhelper: no service name configured; set OTEL_SERVICE_NAME using=${resolved.serviceName}`]);
});

test('R4/ADR-003 deployment.environment.name: code > OTEL_RESOURCE_ATTRIBUTES > ENVIRONMENT; unknown verbatim', () => {
  const cases = [
    [{ ENVIRONMENT: 'prd' }, {}, 'PRD', 'warn'],
    [{ ENVIRONMENT: ' staging ' }, {}, 'staging', 'info'],
    [{ ENVIRONMENT: 'DEV', OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=local' }, {}, 'local', 'debug'],
    [{ OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=DEV' }, { environment: 'Prd' }, 'Prd', 'warn'],
    [{ OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=DEV' }, { resourceAttributes: { 'deployment.environment.name': 'HML' } }, 'HML', 'info'],
  ];
  for (const [env, options, want, level] of cases) {
    resetAndCapture();
    for (const k of IDENTITY_VARS) delete process.env[k];
    setEnv({ OTEL_SERVICE_NAME: 'svc', ...env });
    const resolved = helper.setup(options);
    assert.equal(resolved.environment, want, JSON.stringify(env));
    assert.equal(attrs()['deployment.environment.name'], want, JSON.stringify(env));
    assert.equal(resolved.logLevel, level, JSON.stringify(env));
  }
});

test('R4 environment unset (or blank): attribute absent, one warning, log level info', () => {
  setEnv({ OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name= ', ENVIRONMENT: ' ' });
  const resolved = helper.setup();
  assert.equal(resolved.environment, undefined);
  assert.equal('deployment.environment.name' in attrs(), false);
  assert.equal(resolved.logLevel, 'info');
  const warned = lines((r) => r.line.includes('deployment.environment.name'));
  assert.equal(warned.length, 1, warned.join('\n'));
  assert.match(warned[0], /is not set; set it in OTEL_RESOURCE_ATTRIBUTES/);
});

test('D4 service.instance.id: per-process UUID unless OTEL_RESOURCE_ATTRIBUTES or code set it', () => {
  setEnv({ OTEL_SERVICE_NAME: 'svc' });
  const first = helper.setup();
  assert.match(first.serviceInstanceId, UUID);
  assert.equal(attrs()['service.instance.id'], first.serviceInstanceId);
  resetAndCapture();
  setEnv({ OTEL_RESOURCE_ATTRIBUTES: 'service.instance.id=inst-1' });
  assert.equal(helper.setup().serviceInstanceId, 'inst-1');
  resetAndCapture();
  assert.equal(helper.setup({ resourceAttributes: { 'service.instance.id': 'code-1' } }).serviceInstanceId, 'code-1');
  assert.equal(attrs()['service.instance.id'], 'code-1');
});

test('R5 resource: OTEL_RESOURCE_ATTRIBUTES keys kept, code wins per key, telemetry.sdk.* present, no detectors', () => {
  setEnv({ OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'team.name=payments,region=eu,deployment.environment.name=HML' });
  helper.setup({ resourceAttributes: { region: 'us' } });
  const a = attrs();
  assert.equal(a['team.name'], 'payments');
  assert.equal(a.region, 'us');
  assert.equal(a['deployment.environment.name'], 'HML');
  assert.equal(a['telemetry.sdk.language'], 'nodejs');
  assert.equal(a['telemetry.sdk.name'], 'opentelemetry');
  for (const k of Object.keys(a)) assert.doesNotMatch(k, /^(process|host|os)\./, `${k}: no detector attributes (D-JS4)`);
});

test('JS4 OTEL_RESOURCE_ATTRIBUTES parse errors warn by position, never printing values', () => {
  setEnv({ OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'ok=1,secret-token-abc,k=%zzsecret' });
  helper.setup();
  const a = attrs();
  assert.equal(a.ok, '1');
  assert.equal(a.k, '%zzsecret');
  const raLines = lines((r) => r.line.includes('OTEL_RESOURCE_ATTRIBUTES') && !r.line.includes('deployment.environment.name'));
  assert.deepEqual(raLines, [
    'otelhelper: resource attribute items are not key=value and were skipped variable=OTEL_RESOURCE_ATTRIBUTES items=2',
    'otelhelper: resource attribute values have an invalid percent-encoding and are kept literally variable=OTEL_RESOURCE_ATTRIBUTES items=3',
  ]);
  assert.doesNotMatch(lines().join('\n'), /secret/, 'R8: values never printed');
});

test('R8/D8 deprecated variables warn once per process, naming only the variable; G9 extra instrumentation has no effect', () => {
  setEnv({
    SERVICE_NAME: 'legacy-secret', ENVIRONMENT: 'DEV', OTEL_HELPER_SAMPLE_RATIO: '0.5',
    OTEL_HELPER_DISABLED_SIGNALS: 'logs', OTEL_HELPER_EXTRA_INSTRUMENTATION: 'SQL',
  });
  helper.setup();
  return helper.shutdown().then(() => {
    helper.setup(); // after shutdown: rebuilt, but the warnings are once per process
    for (const v of ['SERVICE_NAME', 'ENVIRONMENT', 'OTEL_HELPER_SAMPLE_RATIO', 'OTEL_HELPER_DISABLED_SIGNALS']) {
      const got = lines((r) => r.line.startsWith(`otelhelper: ${v} is deprecated`));
      assert.equal(got.length, 1, `${v}: ${got.join('\n')}`);
    }
    const extra = lines((r) => r.line.includes('OTEL_HELPER_EXTRA_INSTRUMENTATION'));
    assert.deepEqual(extra, [
      'otelhelper: OTEL_HELPER_EXTRA_INSTRUMENTATION has no effect in Node.js and is ignored; database, Redis and AWS instrumentation come from the opt-in @staffops/otel-helper-sql, -redis and -aws packages variable=OTEL_HELPER_EXTRA_INSTRUMENTATION',
    ]);
    assert.doesNotMatch(lines().join('\n'), /legacy-secret/);
    assert.ok(records.every((r) => r.origin === 'helper'));
  });
});

test('R4 log level: minimumLogLevel wins whatever its value; debug option beats OTEL_HELPER_DEBUG_LEVEL', () => {
  setEnv({ OTEL_SERVICE_NAME: 'svc', ENVIRONMENT: 'LOCAL', OTEL_HELPER_DEBUG_LEVEL: 'true' });
  assert.deepEqual([helper.setup({ minimumLogLevel: 'info' }).logLevel, helper.setup().debug], ['info', true]);
  resetAndCapture();
  setEnv({ ENVIRONMENT: 'PRD' });
  const r = helper.setup({ debug: false });
  assert.deepEqual([r.debug, r.logLevel], [false, 'warn']);
  resetAndCapture();
  assert.deepEqual([helper.setup().debug, helper.setup().logLevel], [true, 'debug']);
  resetAndCapture();
  setEnv({ OTEL_HELPER_DEBUG_LEVEL: 'maybe' });
  assert.equal(helper.setup().debug, false);
  assert.ok(lines().some((l) => l.includes('unparseable boolean variable=OTEL_HELPER_DEBUG_LEVEL')));
});

test('lifecycle (B3 subset): setup is idempotent: first call wins, frozen result, later options ignored with an info record', () => {
  setEnv({ OTEL_SERVICE_NAME: 'svc' });
  const first = helper.setup({ serviceName: 'one' });
  assert.ok(Object.isFrozen(first));
  assert.equal(helper.setup({ serviceName: 'two' }), first);
  assert.equal(helper.setup(), first);
  const info = records.filter((r) => r.line.includes('setup() already ran'));
  assert.deepEqual(info.map((r) => [r.origin, r.level]), [['helper', 'info']]);
});

test('lifecycle (B3 subset): shutdown: cached promise, rebuild on the next setup, bad timeout rejects, nothing active resolves', async () => {
  await helper.shutdown();
  await assert.rejects(helper.shutdown(0), /timeoutMs must be a positive number, got 0/);
  await assert.rejects(helper.shutdown(Number.NaN), TypeError);
  setEnv({ OTEL_SERVICE_NAME: 'svc' });
  const first = helper.setup();
  const p = helper.shutdown();
  assert.equal(helper.shutdown(), p);
  await p;
  const second = helper.setup({ serviceName: 'again' });
  assert.notEqual(second, first);
  assert.equal(second.serviceName, 'again');
  assert.equal(attrs()['service.name'], 'again');
});

test('lifecycle (B3 subset): shutdown rejects when a provider fails or the deadline passes', async () => {
  const failing = { onStart() {}, onEnd() {}, forceFlush: () => Promise.resolve(), shutdown: () => Promise.reject(new Error('boom')) };
  helper.setup({ serviceName: 'svc', spanProcessors: [failing] });
  await assert.rejects(helper.shutdown(), (err) => err instanceof AggregateError && err.errors[0].message === 'boom');
  resetAndCapture();
  const hanging = { onStart() {}, onEnd() {}, forceFlush: () => Promise.resolve(), shutdown: () => new Promise(() => {}) };
  helper.setup({ serviceName: 'svc', spanProcessors: [hanging] });
  await assert.rejects(helper.shutdown(20), /did not finish within 20 ms/);
  resetAndCapture();
  // A provider that throws synchronously still goes through allSettled.
  const throwing = { onStart() {}, onEnd() {}, forceFlush: () => Promise.resolve(), shutdown() { throw new Error('sync boom'); } };
  helper.setup({ serviceName: 'svc', spanProcessors: [throwing] });
  await assert.rejects(helper.shutdown(), (err) => err instanceof AggregateError && /sync boom/.test(String(err.errors[0]?.message)));
  resetAndCapture();
  // A timeout above setTimeout's range is clamped (it would otherwise fire after 1 ms).
  helper.setup({ serviceName: 'svc' });
  await helper.shutdown(Number.MAX_SAFE_INTEGER);
});

// Child processes: the deadline timer is ref'd (amended D-JS10), so the
// process cannot exit silently while a provider hangs; a fire-and-forget
// shutdown of a healthy setup still lets the process exit at once.
const HANGING = "{ onStart() {}, onEnd() {}, forceFlush: () => Promise.resolve(), shutdown: () => new Promise(() => {}) }";
test('lifecycle (B3 subset): a hung provider without any other handle still reports the deadline', () => {
  const res = runCjs(`
    const h = require('@staffops/otel-helper');
    h.setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false, spanProcessors: [${HANGING}] });
    h.shutdown(150).then(() => console.log('resolved'), (e) => console.log('rejected: ' + e.message));
  `);
  assert.equal(res.status, 0, res.stderr);
  assert.equal(res.stdout.trim(), 'rejected: shutdown(): providers did not finish within 150 ms');
});

test('lifecycle (B3 subset): a hung provider while a socket is open rejects at the deadline', () => {
  const res = runCjs(`
    const net = require('node:net');
    const server = net.createServer().listen(0, '127.0.0.1', () => {
      const h = require('@staffops/otel-helper');
      h.setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false, spanProcessors: [${HANGING}] });
      const started = Date.now();
      h.shutdown(150).then(() => console.log('resolved'), (e) => {
        console.log(JSON.stringify({ message: e.message, waited: Date.now() - started >= 100 }));
        server.close();
      });
    });
  `);
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(lastJson(res), { message: 'shutdown(): providers did not finish within 150 ms', waited: true });
});

test('lifecycle (B3 subset): fire-and-forget shutdown of a healthy setup exits promptly and cleanly', () => {
  const started = Date.now();
  const res = runCjs("const h = require('@staffops/otel-helper'); h.setup({ serviceName: 'svc', environment: 'DEV' }); h.shutdown();");
  assert.equal(res.status, 0, res.stderr);
  assert.equal(res.stdout, '');
  assert.equal(res.stderr, '');
  assert.ok(Date.now() - started < 10_000, 'the default 30 s deadline timer is cleared once providers finish');
});

// Decision recorded for B9 (design D-JS10, 2026-10-29): one stable,
// helper-owned forwarding TracerProvider registered once, whose SDK delegate is
// swapped on rebuild. Until then a tracer obtained before a re-setup stays
// bound to the old, shut-down provider; this test pins the target behavior.
test('re-setup keeps tracers obtained earlier working (forwarding provider)', { todo: 'B9: stable forwarding TracerProvider (design D-JS10, 2026-10-29)' }, async () => {
  const before = api.trace.getTracer('early');
  helper.setup({ serviceName: 'svc' });
  before.startSpan('warm').end();
  await helper.shutdown();
  const exporter = new InMemorySpanExporter();
  helper.setup({ serviceName: 'svc', spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  before.startSpan('after-resetup').end();
  assert.deepEqual(exporter.getFinishedSpans().map((s) => s.name), ['after-resetup']);
});

test('lifecycle (B3 subset): reset() unregisters the globals the helper registered', () => {
  helper.setup({ serviceName: 'svc' });
  assert.equal(api.trace.getTracerProvider().getDelegate().constructor.name, 'TracerProvider');
  resetAndCapture();
  assert.equal(api.trace.getTracerProvider().getDelegate().constructor.name, 'NoopTracerProvider');
  assert.equal(api.context.active(), api.ROOT_CONTEXT);
  assert.equal(testing.getResource(), undefined);
  api.diag.warn('nobody listens after reset');
  assert.equal(records.length, 0);
});

test('JS3 reportOnce writes a key once until reset() clears it', () => {
  const diagnostics = require('../dist/internal/diagnostics.js');
  assert.equal(diagnostics.reportOnce('b3-test-key', 'once'), true);
  assert.equal(diagnostics.reportOnce('b3-test-key', 'once'), false);
  assert.deepEqual(lines(), ['otelhelper: once']);
  resetAndCapture();
  assert.equal(diagnostics.reportOnce('b3-test-key', 'once'), true);
});

test('R8 echoed env values are cut to 64 characters; SDK message strings are bounded', () => {
  setEnv({ OTEL_SERVICE_NAME: 'svc', ENVIRONMENT: 'DEV', OTEL_LOG_LEVEL: 'x'.repeat(100), OTEL_HELPER_DEBUG_LEVEL: 'y'.repeat(100) });
  helper.setup();
  assert.ok(lines().includes(`otelhelper: unknown log level variable=OTEL_LOG_LEVEL value=${'x'.repeat(64)}… using=WARN`), lines().join('\n'));
  assert.ok(lines().includes(`otelhelper: unparseable boolean variable=OTEL_HELPER_DEBUG_LEVEL value=${'y'.repeat(64)}… using=false`));
  api.diag.warn('payload', 'z'.repeat(5000));
  api.diag.warn('list', Array.from({ length: 50 }, (_, i) => i));
  const [long, list] = records.filter((r) => r.origin === 'diag').map((r) => r.line);
  assert.ok(long.length < 2200, `bounded: ${long.length}`);
  assert.match(long, /… \(2960 more characters\)"$/);
  assert.match(list, /\.\.\. 30 more items/);
});

test('D-JS7 spanProcessors receive the helper\'s spans; the TracerProvider carries the resolved resource', async () => {
  const exporter = new InMemorySpanExporter();
  helper.setup({ serviceName: 'svc', spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  // Context manager registered (D-JS1): a child started in an active span is linked to it.
  api.trace.getTracer('t').startActiveSpan('parent', (parent) => {
    api.trace.getTracer('t').startSpan('child').end();
    parent.end();
  });
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 2);
  assert.equal(spans[0].resource.attributes['service.name'], 'svc');
  assert.equal(spans[0].parentSpanContext?.spanId, spans[1].spanContext().spanId);
});

test('lifecycle (B3 subset): foreign global TracerProvider / context manager: setup warns and leaves them', () => {
  const foreign = new TracerProvider();
  assert.equal(api.trace.setGlobalTracerProvider(foreign), true);
  const cm = { active: () => api.ROOT_CONTEXT, with: (_c, fn, t, ...a) => fn.call(t, ...a), bind: (_c, t) => t, enable() { return this; }, disable() { return this; } };
  assert.equal(api.context.setGlobalContextManager(cm), true);
  try {
    helper.setup({ serviceName: 'svc', environment: 'DEV' });
    // Exactly the helper's two warnings: no API duplicate-registration error
    // (with its stack trace) forwarded from diag.
    assert.deepEqual(records.map((r) => [r.origin, r.level, r.line]), [
      ['helper', 'warn', 'otelhelper: a global context manager is already registered; the helper keeps it'],
      ['helper', 'warn', 'otelhelper: a global TracerProvider is already registered; the helper does not replace it and its traces are inactive signal=traces'],
    ]);
    assert.equal(api.trace.getTracerProvider().getDelegate(), foreign);
  } finally {
    testing.reset();
    api.trace.disable();
    api.context.disable();
  }
});

test('JS3 DiagLogger: installed at WARN by default, SDK messages forwarded with origin diag, one quoted line', () => {
  helper.setup({ serviceName: 'svc' });
  api.diag.info('not forwarded at WARN');
  api.diag.warn('first line\nsecond line', { k: 'v' });
  api.diag.error('bad', new Error('kaput'));
  const diagRecords = records.filter((r) => r.origin === 'diag');
  assert.deepEqual(diagRecords.map((r) => r.level), ['warn', 'error']);
  assert.equal(diagRecords[0].line, `otelhelper: opentelemetry diag level=warn msg=${JSON.stringify("first line\nsecond line { k: 'v' }")}`);
  assert.ok(diagRecords.every((r) => !r.line.includes('\n')), 'never splits a record');
  assert.match(diagRecords[1].line, /kaput/);
});

test('JS3 OTEL_LOG_LEVEL sets the DiagLogger level; an unknown value warns and uses WARN', () => {
  setEnv({ OTEL_SERVICE_NAME: 'svc', OTEL_LOG_LEVEL: ' debug ' });
  helper.setup();
  api.diag.debug('dbg-probe');
  api.diag.verbose('vrb-probe');
  // The SDK logs its own debug messages too: look at the probes only.
  const probes = () => records.filter((r) => r.origin === 'diag' && /(dbg|vrb)-probe/.test(r.line)).map((r) => r.level);
  assert.deepEqual(probes(), ['debug']);
  resetAndCapture();
  setEnv({ OTEL_LOG_LEVEL: 'all' });
  helper.setup();
  api.diag.verbose('vrb-probe');
  assert.deepEqual(probes(), ['debug'], 'VERBOSE is forwarded at debug level');
  resetAndCapture();
  setEnv({ OTEL_LOG_LEVEL: 'chatty' });
  helper.setup();
  assert.ok(lines().includes('otelhelper: unknown log level variable=OTEL_LOG_LEVEL value=chatty using=WARN'));
  api.diag.info('no');
  api.diag.warn('yes');
  assert.deepEqual(records.filter((r) => r.origin === 'diag').map((r) => r.level), ['warn']);
  resetAndCapture();
  setEnv({ OTEL_LOG_LEVEL: 'NONE' });
  helper.setup();
  api.diag.error('silenced');
  assert.equal(records.filter((r) => r.origin === 'diag').length, 0);
});

test('JS3 installDiagLogger:false on a re-setup removes the logger an earlier setup installed', async () => {
  helper.setup({ serviceName: 'svc', environment: 'DEV' });
  await helper.shutdown();
  helper.setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false });
  api.diag.warn('nobody');
  assert.equal(records.filter((r) => r.origin === 'diag').length, 0);
  // An app logger set meanwhile is not the helper's: a later setup warns about overriding it.
  await helper.shutdown();
  const appLines = [];
  api.diag.setLogger({ error() {}, warn: (m) => appLines.push(m), info() {}, debug() {}, verbose() {} }, api.DiagLogLevel.WARN);
  helper.setup({ serviceName: 'svc', environment: 'DEV' });
  assert.ok(appLines.some((m) => /overwritten/.test(m)), 'suppressOverrideMessage only when diag still holds the helper logger');
});

test('JS3 installDiagLogger:false leaves diag alone; re-setup after shutdown suppresses the override message', async () => {
  helper.setup({ serviceName: 'svc', installDiagLogger: false });
  api.diag.warn('to nobody');
  assert.equal(records.filter((r) => r.origin === 'diag').length, 0);
  resetAndCapture();
  helper.setup({ serviceName: 'svc' });
  await helper.shutdown();
  helper.setup({ serviceName: 'svc' });
  assert.equal(records.filter((r) => /overwritten|Current logger/i.test(r.line)).length, 0, lines().join('\n'));
  api.diag.warn('still forwarded');
  assert.equal(records.filter((r) => r.origin === 'diag').length, 1);
});

test('JS3 an app DiagLogger set before setup is replaced; the API warns on both (documented)', () => {
  const appLines = [];
  const appLogger = { error: (m) => appLines.push(m), warn: (m) => appLines.push(m), info() {}, debug() {}, verbose() {} };
  api.diag.setLogger(appLogger, api.DiagLogLevel.WARN);
  helper.setup({ serviceName: 'svc' });
  assert.ok(appLines.some((m) => /overwritten/i.test(m)), appLines.join('\n'));
  assert.ok(records.some((r) => r.origin === 'diag' && /Current logger will overwrite/.test(r.line)));
});

test('JS3 default sink: helper records on stderr only, one line each, nothing on stdout', () => {
  const res = runCjs("require('@staffops/otel-helper').setup(); require('@opentelemetry/api').diag.warn('sdk says hi')", {
    env: { OTEL_SERVICE_NAME: 'svc', ENVIRONMENT: '', OTEL_RESOURCE_ATTRIBUTES: '', SERVICE_NAME: '', OTEL_LOG_LEVEL: '' },
  });
  assert.equal(res.status, 0, res.stderr);
  assert.equal(res.stdout, '');
  const err = res.stderr.trim().split('\n');
  assert.ok(err.every((l) => l.startsWith('otelhelper: ')), res.stderr);
  assert.ok(err.some((l) => l.includes('deployment.environment.name is not set')));
  assert.ok(err.some((l) => l === 'otelhelper: opentelemetry diag level=warn msg="sdk says hi"'));
});

test('D-JS10 another version owns the process: setup resolves and returns, but installs nothing', () => {
  const res = runCjs(`
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'), { value: { version: '9.9.9' } });
    const h = require('@staffops/otel-helper');
    const t = require('@staffops/otel-helper/testing');
    const r = h.setup({ serviceName: 'svc' });
    const api = require('@opentelemetry/api');
    h.shutdown().then(() => {
      t.reset();
      console.log(JSON.stringify({ name: r.serviceName, resource: t.getResource() === undefined, delegate: api.trace.getTracerProvider().getDelegate().constructor.name }));
    });
  `, { env: { OTEL_SERVICE_NAME: '', ENVIRONMENT: '', SERVICE_NAME: 'legacy', OTEL_RESOURCE_ATTRIBUTES: 'broken' } });
  assert.deepEqual(lastJson(res), { name: 'svc', resource: true, delegate: 'NoopTracerProvider' });
  assert.match(res.stderr, /another version is already loaded/);
  // Quiet: no per-setup identity/resource warnings for a copy that installs nothing; deprecations stay.
  assert.match(res.stderr, /SERVICE_NAME is deprecated/);
  assert.doesNotMatch(res.stderr, /deployment\.environment\.name is not set|not key=value/);
});

test('the ESM entry exposes the same setup()/shutdown()/defaultLogLevel()', async () => {
  const esm = await import('@staffops/otel-helper');
  assert.equal(esm.setup, helper.setup);
  assert.equal(esm.shutdown, helper.shutdown);
  assert.equal(esm.defaultLogLevel, helper.defaultLogLevel);
  const esmTesting = await import('@staffops/otel-helper/testing');
  assert.equal(esmTesting.getResource, testing.getResource);
});
