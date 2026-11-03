'use strict';
// setup()/shutdown(): identity resolution (task B3; spec JS4, contract R4/R5,
// D1, D4, ADR-003), deprecations (D8, Go G9), diagnostics and the DiagLogger
// (spec JS3, design D-JS5) and the lifecycle (task B9; spec JS7, design
// D-JS10): forwarding providers across re-setups, the G1 rollback, foreign
// globals and replaceGlobals.
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('@opentelemetry/api');
const { TracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const http = require('node:http');
const net = require('node:net');
const zlib = require('node:zlib');
const { runCjs, lastJson } = require('./helpers.cjs');

const IDENTITY_VARS = [
  'OTEL_NODE_RESOURCE_DETECTORS', 'OTEL_SERVICE_NAME', 'OTEL_RESOURCE_ATTRIBUTES', 'SERVICE_NAME', 'ENVIRONMENT', 'OTEL_HELPER_DEBUG_LEVEL',
  'OTEL_HELPER_SAMPLE_RATIO', 'OTEL_HELPER_DISABLED_SIGNALS', 'OTEL_HELPER_EXTRA_INSTRUMENTATION', 'OTEL_LOG_LEVEL',
];
// Children inherit it: never bind :9464 from unit tests (test/metrics.test.cjs binds its own).
process.env.OTEL_HELPER_METRICS_PORT = '0';
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

test('D1 2026-10-29: telemetry.sdk.* come from the SDK (env and code ignored); schema URL is the contract literal', () => {
  setEnv({
    OTEL_SERVICE_NAME: 'svc',
    OTEL_RESOURCE_ATTRIBUTES: 'telemetry.sdk.name=env,telemetry.sdk.language=env,telemetry.distro.name=d',
  });
  helper.setup({ resourceAttributes: { 'telemetry.sdk.version': 'code' } });
  const a = attrs();
  const sdkVersion = require('@opentelemetry/resources').defaultResource().attributes['telemetry.sdk.version'];
  assert.deepEqual(
    [a['telemetry.sdk.name'], a['telemetry.sdk.language'], a['telemetry.sdk.version'], a['telemetry.distro.name']],
    ['opentelemetry', 'nodejs', sdkVersion, 'd'],
  );
  assert.equal(testing.getResource().schemaUrl, 'https://opentelemetry.io/schemas/1.43.0');
});

test('D1 2026-10-29: identity and parse warnings are written once per process, not per setup', async () => {
  setEnv({ OTEL_RESOURCE_ATTRIBUTES: 'broken,k=%zz' });
  helper.setup();
  await helper.shutdown();
  helper.setup();
  const count = (needle) => lines((r) => r.line.includes(needle)).length;
  assert.equal(count('no service name configured'), 1);
  assert.equal(count('deployment.environment.name is not set'), 1);
  assert.equal(count('not key=value'), 1);
  assert.equal(count('invalid percent-encoding'), 1);
});

test('D1 2026-10-29: code telemetry.sdk.* keys warn once by key; env values are silent', async () => {
  setEnv({ OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'telemetry.sdk.name=from-env' });
  helper.setup({ resourceAttributes: { 'telemetry.sdk.version': 's3cr3t' } });
  await helper.shutdown();
  helper.setup({ resourceAttributes: { 'telemetry.sdk.version': 's3cr3t' } });
  const sdkLines = lines((r) => r.line.includes('telemetry.sdk'));
  assert.equal(sdkLines.length, 1, sdkLines.join('\n'));
  assert.match(sdkLines[0], /telemetry\.sdk\.version in resourceAttributes is ignored/);
  assert.doesNotMatch(sdkLines[0], /s3cr3t|from-env/);
  assert.notEqual(attrs()['telemetry.sdk.version'], 's3cr3t');
});

test('D1 2026-10-29: OTEL_NODE_RESOURCE_DETECTORS is ignored and warned once (name only)', async () => {
  for (const value of ['env', 'env, serviceinstance', 'none']) {
    resetAndCapture();
    setEnv({ OTEL_SERVICE_NAME: 'svc', OTEL_NODE_RESOURCE_DETECTORS: value });
    helper.setup();
    await helper.shutdown();
    assert.equal(lines((r) => r.line.includes('OTEL_NODE_RESOURCE_DETECTORS')).length, 0, value);
  }
  resetAndCapture();
  setEnv({ OTEL_SERVICE_NAME: 'svc', OTEL_NODE_RESOURCE_DETECTORS: 'env,host,process' });
  helper.setup();
  await helper.shutdown();
  helper.setup();
  const det = lines((r) => r.line.includes('OTEL_NODE_RESOURCE_DETECTORS'));
  assert.equal(det.length, 1);
  assert.doesNotMatch(det[0], /host,process/);
  for (const k of Object.keys(attrs())) assert.doesNotMatch(k, /^(process|host)\./);
});

test('D1 2026-10-29: a __proto__ key stays an ordinary attribute up to the SDK resource (env and code)', () => {
  // The helper keeps it as an own data property; @opentelemetry/resources 3.0's
  // Resource.attributes getter then drops it (attrs[k] = v on {}) — D-JS14.
  const raw = () => Object.fromEntries(testing.getResource().getRawAttributes().filter(([k]) => k === '__proto__'));
  setEnv({ OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: '__proto__=from-env,team.name=payments' });
  helper.setup();
  assert.deepEqual(Object.getOwnPropertyDescriptor(raw(), '__proto__')?.value, 'from-env');
  assert.equal(attrs()['team.name'], 'payments');
  assert.equal(Object.getPrototypeOf(testing.getResource().attributes), Object.prototype, 'no prototype write');
  resetAndCapture();
  helper.setup({ resourceAttributes: JSON.parse('{"__proto__": "from-code"}') });
  assert.equal(Object.getOwnPropertyDescriptor(raw(), '__proto__')?.value, 'from-code');
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

// Design D-JS10 (decided 2026-10-29, implemented in B9): one stable,
// helper-owned forwarding TracerProvider registered once, whose SDK delegate
// is swapped on rebuild, so a tracer obtained before a re-setup keeps working.
test('JS7/D-JS10 re-setup keeps tracers obtained earlier working (forwarding provider)', async () => {
  const before = api.trace.getTracer('early');
  helper.setup({ serviceName: 'svc' });
  before.startSpan('warm').end();
  await helper.shutdown();
  const exporter = new InMemorySpanExporter();
  helper.setup({ serviceName: 'svc', spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  before.startSpan('after-resetup').end();
  assert.deepEqual(exporter.getFinishedSpans().map((s) => s.name), ['after-resetup']);
});

test('JS7/D-JS10 meters, instruments and Logs API loggers obtained before a re-setup keep working', async () => {
  const options = { serviceName: 'svc', metricExporters: ['prometheus'], prometheusMetricsPort: 0 };
  helper.setup(options);
  const meter = api.metrics.getMeter('early');
  const counter = meter.createCounter('early_count');
  meter.createObservableGauge('early_gauge').addCallback((result) => result.observe(3));
  const logger = api.logs.getLogger('early');
  counter.add(1);
  logger.emit({ body: 'first' });
  await helper.shutdown();
  counter.add(100); // after shutdown: the API no-op
  helper.setup(options);
  counter.add(2);
  const { meterProvider, loggerProvider } = testing.getProviders();
  const { resourceMetrics } = await meterProvider._sharedState.metricCollectors[0]._metricReader.collect();
  const points = Object.fromEntries(
    resourceMetrics.scopeMetrics.flatMap((s) => s.metrics).filter((m) => m.descriptor.name.startsWith('early_')).map((m) => [m.descriptor.name, m.dataPoints.map((p) => p.value)]),
  );
  assert.deepEqual(points, { early_count: [2], early_gauge: [3] });
  const emitted = [];
  loggerProvider.getLogger = () => ({ emit: (record) => emitted.push(record.body), enabled: () => true });
  logger.emit({ body: 'after-resetup' });
  assert.deepEqual(emitted, ['after-resetup']);
});

test('JS7/H1 the forwarders keep each provider until its shutdown settles, then forward to nothing; the next setup records again', async () => {
  const tracer = api.trace.getTracer('t');
  helper.setup({ serviceName: 'svc' });
  assert.equal(tracer.startSpan('a').isRecording(), true);
  const sdk = testing.getProviders();
  assert.ok(Object.isFrozen(sdk));
  const forwarders = [api.trace.getTracerProvider().getDelegate(), api.metrics.getMeterProvider(), api.logs.getLoggerProvider()];
  const pending = helper.shutdown();
  assert.deepEqual(forwarders.map((f) => f.getDelegate()), [sdk.tracerProvider, sdk.meterProvider, sdk.loggerProvider]);
  assert.equal(testing.getProviders(), undefined, 'shutting down: no active providers');
  await pending;
  assert.deepEqual(forwarders.map((f) => f.getDelegate()), [undefined, undefined, undefined]);
  assert.equal(tracer.startSpan('b').isRecording(), false);
  helper.setup({ serviceName: 'svc' });
  assert.equal(tracer.startSpan('c').isRecording(), true);
});

test('JS7/H1 a delegate a newer setup replaced is not cleared by the older shutdown', async () => {
  let release;
  const slow = { onStart() {}, onEnd() {}, forceFlush: () => Promise.resolve(), shutdown: () => new Promise((r) => { release = r; }) };
  helper.setup({ serviceName: 'svc', spanProcessors: [slow] });
  const forwarder = api.trace.getTracerProvider().getDelegate();
  const pending = helper.shutdown();
  helper.setup({ serviceName: 'svc' });
  const newer = testing.getProviders().tracerProvider;
  while (release === undefined) await new Promise((r) => setImmediate(r));
  release();
  await pending;
  assert.equal(forwarder.getDelegate(), newer);
});

test('M1 forceFlush() on the global providers flushes the active SDK providers', async () => {
  let flushed = 0;
  const probe = { onStart() {}, onEnd() {}, forceFlush: async () => { flushed++; }, shutdown: () => Promise.resolve() };
  helper.setup({ serviceName: 'svc', spanProcessors: [probe] });
  await api.trace.getTracerProvider().getDelegate().forceFlush();
  await api.metrics.getMeterProvider().forceFlush();
  await api.logs.getLoggerProvider().forceFlush();
  assert.equal(flushed, 1);
});

/** A local OTLP/HTTP receiver: every POST body (gunzipped) by path. */
async function otlpSink() {
  const posts = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks);
      posts.push({ path: req.url, body: req.headers['content-encoding'] === 'gzip' ? zlib.gunzipSync(raw) : raw });
      res.writeHead(200, { 'content-type': 'application/x-protobuf' });
      res.end();
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { posts, port: server.address().port, close: () => new Promise((r) => server.close(r)) };
}

test('H1 regression: observable and batch callbacks on a global meter reach the final OTLP export of shutdown(), also after a re-setup', async () => {
  const sink = await otlpSink();
  try {
    const options = {
      serviceName: 'svc', otelEndpoint: `http://127.0.0.1:${sink.port}`, otlpProtocol: 'http/protobuf',
      metricExporters: ['otlp'], exportIntervalMs: 600_000,
    };
    helper.setup(options);
    const meter = api.metrics.getMeter('h1');
    meter.createObservableGauge('h1_gauge').addCallback((r) => r.observe(1));
    const batched = meter.createObservableGauge('h1_batch');
    meter.addBatchObservableCallback((r) => r.observe(batched, 2), [batched]);
    const exported = () => sink.posts.filter((p) => p.path === '/v1/metrics').map((p) => p.body.toString('latin1'));
    // Before the first interval: only the final collection of shutdown() exports.
    await helper.shutdown();
    assert.equal(exported().length, 1);
    assert.ok(exported()[0].includes('h1_gauge') && exported()[0].includes('h1_batch'));
    helper.setup(options);
    await helper.shutdown();
    assert.equal(exported().length, 2);
    assert.ok(exported()[1].includes('h1_gauge') && exported()[1].includes('h1_batch'));
  } finally {
    await sink.close();
  }
});

// ── G1 rollback, foreign globals, replaceGlobals (JS7, task B9) ──────────

const { registeredGlobal } = require('../dist/internal/globals.js');
const { setupFault, logDestination } = require('../dist/internal/setup.js');

const STEPS = ['diag', 'pipelines', 'built', 'context', 'propagator', 'providers', 'instrumentations', 'listener'];
const { MeterProvider, MetricReader } = require('@opentelemetry/sdk-metrics');
const { LoggerProvider } = require('@opentelemetry/sdk-logs');

/** Counts shutdown() calls on every MeterProvider, LoggerProvider and metric reader while `fn` runs (M5). */
async function countingShutdowns(fn) {
  const counts = { meterProvider: 0, loggerProvider: 0, reader: 0 };
  const patches = [[MeterProvider.prototype, 'meterProvider'], [LoggerProvider.prototype, 'loggerProvider'], [MetricReader.prototype, 'reader']];
  const originals = patches.map(([proto]) => proto.shutdown);
  patches.forEach(([proto, key], i) => {
    proto.shutdown = function (...args) {
      counts[key]++;
      return originals[i].apply(this, args);
    };
  });
  try {
    await fn(counts);
  } finally {
    patches.forEach(([proto], i) => {
      proto.shutdown = originals[i];
    });
  }
}
const GLOBALS = ['trace', 'metrics', 'logs', 'context', 'propagation'];
const present = () => GLOBALS.filter((g) => registeredGlobal(g) !== undefined);
const snapshot = () => Object.fromEntries(GLOBALS.map((g) => [g, registeredGlobal(g)]));

/** Runs `fn` with setup() failing at `step`. */
function failingAt(step, fn) {
  setupFault.at = (s) => {
    if (s === step) throw new Error(`injected at ${s}`);
  };
  try {
    return fn();
  } finally {
    setupFault.at = undefined;
  }
}

/** The status the mounted /metrics handler answers now. */
function scrapeStatus() {
  return new Promise((resolve) => {
    const res = {
      statusCode: 200,
      headersSent: false,
      setHeader() {},
      getHeader() {},
      removeHeader() {},
      writeHead(status) {
        this.statusCode = status;
        return this;
      },
      write() {
        return true;
      },
      end() {
        resolve(this.statusCode);
        return this;
      },
    };
    helper.metricsHandler()({ method: 'GET', url: '/metrics', headers: {} }, res);
  });
}

function freePort() {
  return new Promise((resolve) => {
    const server = net.createServer().listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

/** Whether `port` can be bound (retried briefly: a closing listener frees it asynchronously). */
async function bindable(port) {
  for (let i = 0; i < 50; i++) {
    const ok = await new Promise((resolve) => {
      const server = net.createServer();
      server.once('error', () => resolve(false));
      server.listen(port, () => server.close(() => resolve(true)));
    });
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 20));
  }
  return false;
}

function injected() {
  const carrier = {};
  const span = api.trace.setSpanContext(api.ROOT_CONTEXT, { traceId: '0af7651916cd43dd8448eb211c80319c', spanId: 'b7ad6b7169203331', traceFlags: 1 });
  api.propagation.inject(api.propagation.setBaggage(span, api.propagation.createBaggage({ k: { value: 'v' } })), carrier);
  return Object.keys(carrier).sort();
}

test('G1 a setup failing at any step throws, releases what it built and leaves the globals as they were', async () => {
  const port = await freePort();
  for (const step of STEPS) {
    resetAndCapture();
    let probeShutdowns = 0;
    const probe = { onStart() {}, onEnd() {}, forceFlush: () => Promise.resolve(), shutdown: async () => { probeShutdowns++; } };
    await countingShutdowns(async (counts) => {
      assert.throws(
        () => failingAt(step, () => helper.setup({ serviceName: 'svc', metricExporters: ['prometheus'], prometheusMetricsPort: port, spanProcessors: [probe] })),
        { message: `injected at ${step}` },
      );
      await new Promise((r) => setImmediate(r));
      // M5: from `built` on, the three providers are shut down (the reader with its MeterProvider);
      // at `pipelines` (no provider yet) the pipelines alone are.
      const built = STEPS.indexOf(step) >= STEPS.indexOf('built');
      assert.deepEqual(
        { probe: probeShutdowns, ...counts },
        built ? { probe: 1, meterProvider: 1, loggerProvider: 1, reader: 1 } : { probe: 0, meterProvider: 0, loggerProvider: 0, reader: step === 'pipelines' ? 1 : 0 },
        step,
      );
    });
    assert.deepEqual(present(), [], step);
    assert.equal(registeredGlobal('diag'), undefined, step);
    assert.notEqual(http.request.__wrapped, true, step);
    assert.equal(await scrapeStatus(), 503, step);
    assert.equal(logDestination().provider, undefined, step);
    assert.equal(testing.getResource(), undefined, step);
    assert.equal(testing.getProviders(), undefined, step);
    assert.ok(await bindable(port), `${step}: the listener was closed`);
    // Nothing was recorded: the next setup runs in full.
    const resolved = helper.setup({ serviceName: 'svc' });
    assert.equal(resolved.serviceName, 'svc');
    assert.deepEqual(present(), GLOBALS, step);
    await helper.shutdown();
  }
});

test('G1 a failed re-setup after shutdown keeps the helper\'s globals and the last propagator; the forwarders point at nothing', async () => {
  const saved = process.env.OTEL_PROPAGATORS;
  process.env.OTEL_PROPAGATORS = 'baggage';
  try {
    helper.setup({ serviceName: 'svc' });
    await helper.shutdown();
    const before = snapshot();
    delete process.env.OTEL_PROPAGATORS;
    assert.throws(() => failingAt('listener', () => helper.setup({ serviceName: 'svc' })), /injected at listener/);
    assert.deepEqual(snapshot(), before);
    assert.deepEqual(injected(), ['baggage']);
    assert.equal(api.trace.getTracer('t').startSpan('x').isRecording(), false);
    helper.setup({ serviceName: 'svc' });
    assert.deepEqual(injected(), ['baggage', 'traceparent']);
    assert.equal(api.trace.getTracer('t').startSpan('y').isRecording(), true);
  } finally {
    if (saved === undefined) delete process.env.OTEL_PROPAGATORS;
    else process.env.OTEL_PROPAGATORS = saved;
  }
});

test('G1 a failed setup puts back the DiagLogger it replaced (an app one, or the helper\'s from an earlier setup)', async () => {
  const seen = [];
  const app = { error: (m) => seen.push(m), warn: (m) => seen.push(m), info() {}, debug() {}, verbose() {} };
  api.diag.setLogger(app, api.DiagLogLevel.WARN);
  try {
    assert.throws(() => failingAt('built', () => helper.setup({ serviceName: 'svc' })), /injected/);
    records.length = 0;
    api.diag.warn('to the app');
    assert.ok(seen.includes('to the app'));
    assert.equal(records.filter((r) => r.origin === 'diag').length, 0);
  } finally {
    api.diag.disable();
  }
  // The helper's own logger from an earlier setup stays the helper's: a later opt-out removes it.
  helper.setup({ serviceName: 'svc' });
  await helper.shutdown();
  assert.throws(() => failingAt('built', () => helper.setup({ serviceName: 'svc' })), /injected/);
  api.diag.warn('to the helper');
  assert.ok(records.some((r) => r.origin === 'diag' && r.line.includes('to the helper')));
  helper.setup({ serviceName: 'svc', installDiagLogger: false });
  assert.equal(registeredGlobal('diag'), undefined);
  await helper.shutdown();
  // Opted out and failing: diag was never touched.
  assert.throws(() => failingAt('diag', () => helper.setup({ serviceName: 'svc', installDiagLogger: false })), /injected/);
  assert.equal(registeredGlobal('diag'), undefined);
});

test('G1 an undo step that throws is reported; the setup error is the one thrown', () => {
  const res = runCjs(`
    const key = Symbol.for('opentelemetry.js.api.1');
    const { loadedApiVersion } = require('./dist/internal/globals.js');
    globalThis[key] = new Proxy({ version: loadedApiVersion() }, {
      deleteProperty(t, p) { if (p === 'propagation') throw new Error('sticky'); delete t[p]; return true; },
    });
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
    require('./dist/internal/setup.js').setupFault.at = (s) => { if (s === 'providers') throw new Error('injected'); };
    let threw;
    try { require('@staffops/otel-helper').setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false }); } catch (e) { threw = e.message; }
    console.log(JSON.stringify({ threw, lines: lines.filter((l) => l.includes('rollback')) }));
  `);
  assert.deepEqual(lastJson(res), { threw: 'injected', lines: ['otelhelper: setup() rollback: a step could not be undone error=sticky'] });
});

test('JS7 foreign providers are kept, warned and listed in inactiveSignals; a foreign LoggerProvider does not list logs', () => {
  api.trace.setGlobalTracerProvider(new TracerProvider());
  api.metrics.setGlobalMeterProvider({ getMeter: () => api.createNoopMeter() });
  api.logs.setGlobalLoggerProvider({ getLogger: () => api.createNoopLogger() });
  try {
    const resolved = helper.setup({ serviceName: 'svc' });
    assert.deepEqual(resolved.inactiveSignals, ['traces', 'metrics']);
    assert.ok(Object.isFrozen(resolved.inactiveSignals));
    assert.equal(helper.setup(), resolved);
  } finally {
    testing.reset();
    api.trace.disable();
    api.metrics.disable();
    api.logs.disable();
  }
  assert.deepEqual(helper.setup({ serviceName: 'svc' }).inactiveSignals, []);
});

test('JS7 replaceGlobals replaces foreign providers and the propagator, never the context manager; the instrumentations move to the helper\'s', async () => {
  const foreignTrace = new TracerProvider();
  const foreignMeter = { getMeter: () => api.createNoopMeter() };
  const foreignLogs = { getLogger: () => api.createNoopLogger() };
  const foreignPropagator = { inject: (_c, carrier) => { carrier.foreign = '1'; }, extract: (c) => c, fields: () => ['foreign'] };
  const cm = { active: () => api.ROOT_CONTEXT, with: (_c, fn, t, ...a) => fn.call(t, ...a), bind: (_c, t) => t, enable() { return this; }, disable() { return this; } };
  api.trace.setGlobalTracerProvider(foreignTrace);
  api.metrics.setGlobalMeterProvider(foreignMeter);
  api.logs.setGlobalLoggerProvider(foreignLogs);
  api.propagation.setGlobalPropagator(foreignPropagator);
  api.context.setGlobalContextManager(cm);
  const httpInstrumentation = () => testing.getState().runtime.instrumentations.instrumentations[0];
  try {
    // Without replaceGlobals the instrumentations bind to the foreign provider.
    helper.setup({ serviceName: 'svc' });
    assert.equal(httpInstrumentation().tracer.constructor.name, 'Tracer');
    assert.deepEqual(injected(), ['foreign']);
    await helper.shutdown();
    records.length = 0;
    const resolved = helper.setup({ serviceName: 'svc', replaceGlobals: true });
    assert.deepEqual(resolved.inactiveSignals, []);
    assert.equal(api.trace.getTracerProvider().getDelegate().constructor.name, 'ForwardingTracerProvider');
    assert.equal(api.metrics.getMeterProvider().constructor.name, 'ForwardingMeterProvider');
    assert.equal(api.logs.getLoggerProvider().constructor.name, 'ForwardingLoggerProvider');
    assert.deepEqual(injected(), ['baggage', 'traceparent']);
    assert.equal(registeredGlobal('context'), cm);
    assert.equal(httpInstrumentation().tracer.constructor.name, 'ForwardingTracer');
    assert.deepEqual(records.filter((r) => r.level === 'info' && r.line.includes('replaceGlobals')).map((r) => r.line), [
      'otelhelper: a global propagator is already registered; replaceGlobals is set: the helper replaces it',
      'otelhelper: a global TracerProvider is already registered; replaceGlobals is set: the helper replaces it signal=traces',
      'otelhelper: a global MeterProvider is already registered; replaceGlobals is set: the helper replaces it signal=metrics',
      'otelhelper: a global LoggerProvider is already registered; replaceGlobals is set: the helper replaces it signal=logs',
    ]);
    // Once per process under replaceGlobals (B9 fix round).
    const kept = 'otelhelper: a global context manager is already registered; the helper keeps it; replaceGlobals never replaces the context manager';
    assert.deepEqual(lines().filter((l) => l.includes('context manager')), [kept]);
    await helper.shutdown();
    records.length = 0;
    helper.setup({ serviceName: 'svc', replaceGlobals: true });
    assert.deepEqual(lines().filter((l) => l.includes('context manager')), []);
  } finally {
    testing.reset();
    for (const g of ['trace', 'metrics', 'logs', 'propagation', 'context']) api[g].disable();
  }
});

test('G1 a failed replaceGlobals setup puts the foreign globals back', () => {
  const foreignTrace = new TracerProvider();
  const foreignMeter = { getMeter: () => api.createNoopMeter() };
  const foreignLogs = { getLogger: () => api.createNoopLogger() };
  const foreignPropagator = { inject() {}, extract: (c) => c, fields: () => [] };
  api.trace.setGlobalTracerProvider(foreignTrace);
  api.metrics.setGlobalMeterProvider(foreignMeter);
  api.logs.setGlobalLoggerProvider(foreignLogs);
  api.propagation.setGlobalPropagator(foreignPropagator);
  try {
    assert.throws(() => failingAt('providers', () => helper.setup({ serviceName: 'svc', replaceGlobals: true })), /injected/);
    assert.equal(api.trace.getTracerProvider().getDelegate(), foreignTrace);
    assert.equal(api.metrics.getMeterProvider(), foreignMeter);
    assert.equal(registeredGlobal('logs'), foreignLogs);
    assert.equal(registeredGlobal('propagation'), foreignPropagator);
    // The context manager the failed setup registered is gone again.
    assert.equal(registeredGlobal('context'), undefined);
  } finally {
    testing.reset();
    for (const g of ['trace', 'metrics', 'logs', 'propagation']) api[g].disable();
  }
});

test('JS7 replaceGlobals: a replacement the API refuses is reported and the foreign global put back', () => {
  const res = runCjs(`
    const key = Symbol.for('opentelemetry.js.api.1');
    const { loadedApiVersion } = require('./dist/internal/globals.js');
    const sticky = { getMeter: () => require('@opentelemetry/api').createNoopMeter() };
    // A registry whose MeterProvider can be neither removed nor replaced.
    globalThis[key] = new Proxy({ version: loadedApiVersion() }, {
      get(t, p) { return p === 'metrics' ? sticky : t[p]; },
      deleteProperty(t, p) { if (p !== 'metrics') delete t[p]; return true; },
    });
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l, m) => { if (m.origin === 'helper') lines.push(l); });
    const r = require('@staffops/otel-helper').setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false, replaceGlobals: true });
    console.log(JSON.stringify({ inactive: r.inactiveSignals, same: require('@opentelemetry/api').metrics.getMeterProvider() === sticky, lines: lines.filter((l) => l.includes('MeterProvider')) }));
  `);
  assert.deepEqual(lastJson(res), {
    inactive: ['metrics'],
    same: true,
    lines: [
      'otelhelper: a global MeterProvider is already registered; replaceGlobals is set: the helper replaces it signal=metrics',
      "otelhelper: the OpenTelemetry API refused the global MeterProvider; the helper's metrics are inactive signal=metrics",
    ],
  });
});

test('JS7 a global the helper registered and someone replaced is "no longer the helper\'s"; one only removed is registered again', async () => {
  helper.setup({ serviceName: 'svc' });
  await helper.shutdown();
  api.trace.disable();
  api.trace.setGlobalTracerProvider(new TracerProvider());
  api.metrics.disable();
  try {
    const resolved = helper.setup({ serviceName: 'svc' });
    assert.deepEqual(resolved.inactiveSignals, ['traces']);
    assert.ok(lines().includes("otelhelper: the global TracerProvider is no longer the helper's; the helper does not replace it and its traces are inactive signal=traces"), lines().join('\n'));
    assert.equal(api.metrics.getMeterProvider().constructor.name, 'ForwardingMeterProvider');
    assert.ok(!lines().some((l) => l.includes('MeterProvider')));
  } finally {
    testing.reset();
    api.trace.disable();
  }
});

test('lifecycle: reset() unregisters the globals the helper registered', () => {
  helper.setup({ serviceName: 'svc' });
  assert.equal(api.trace.getTracerProvider().getDelegate().constructor.name, 'ForwardingTracerProvider');
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
      console.log(JSON.stringify({ name: r.serviceName, inactive: r.inactiveSignals, resource: t.getResource() === undefined, delegate: api.trace.getTracerProvider().getDelegate().constructor.name }));
    });
  `, { env: { OTEL_SERVICE_NAME: '', ENVIRONMENT: '', SERVICE_NAME: 'legacy', OTEL_RESOURCE_ATTRIBUTES: 'broken' } });
  assert.deepEqual(lastJson(res), { name: 'svc', inactive: ['traces', 'metrics', 'logs'], resource: true, delegate: 'NoopTracerProvider' });
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

test('M3 a failed setup leaves the API proxies alone: a tracer and a Logs API logger obtained before it join the app\'s own SDK', () => {
  const tracer = api.trace.getTracer('early');
  const logger = api.logs.getLogger('early');
  assert.throws(() => failingAt('providers', () => helper.setup({ serviceName: 'svc' })), /injected/);
  const spans = new InMemorySpanExporter();
  const { LoggerProvider: AppLoggerProvider, InMemoryLogRecordExporter, SimpleLogRecordProcessor } = require('@opentelemetry/sdk-logs');
  const logRecords = new InMemoryLogRecordExporter();
  api.trace.setGlobalTracerProvider(new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter: spans })] }));
  api.logs.setGlobalLoggerProvider(new AppLoggerProvider({ processors: [new SimpleLogRecordProcessor({ exporter: logRecords })] }));
  try {
    tracer.startSpan('app-span').end();
    logger.emit({ body: 'app-log' });
    assert.deepEqual(spans.getFinishedSpans().map((x) => x.name), ['app-span']);
    assert.deepEqual(logRecords.getFinishedLogRecords().map((r) => r.body), ['app-log']);
  } finally {
    api.trace.disable();
    api.logs.disable();
  }
});

test('M4 a registry that throws during a replaceGlobals replacement is rolled back: the foreign provider comes back', () => {
  const res = runCjs(`
    const key = Symbol.for('opentelemetry.js.api.1');
    const { loadedApiVersion } = require('./dist/internal/globals.js');
    const foreign = { getMeter: () => require('@opentelemetry/api').createNoopMeter() };
    let thrown = false;
    globalThis[key] = new Proxy({ version: loadedApiVersion(), metrics: foreign }, {
      set(t, p, v) { if (p === 'metrics' && !thrown) { thrown = true; throw new Error('registry set failed'); } t[p] = v; return true; },
    });
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
    let threw;
    try { require('@staffops/otel-helper').setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false, replaceGlobals: true }); } catch (e) { threw = e.message; }
    const reg = globalThis[key];
    console.log(JSON.stringify({ threw, back: reg.metrics === foreign, others: ['trace', 'logs', 'context', 'propagation'].filter((k) => reg[k] !== undefined) }));
  `);
  assert.deepEqual(lastJson(res), { threw: 'registry set failed', back: true, others: [] });
});

test('G1/L the rollback report redacts URL credentials and header-like values', () => {
  const res = runCjs(`
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
    const key = Symbol.for('opentelemetry.js.api.1');
    const { loadedApiVersion } = require('./dist/internal/globals.js');
    globalThis[key] = new Proxy({ version: loadedApiVersion() }, {
      deleteProperty(t, p) { if (p === 'propagation') throw new Error('to https://user:pw@host.example.com/v1?token=abc Authorization: Bearer xyz'); delete t[p]; return true; },
    });
    require('./dist/internal/setup.js').setupFault.at = (s) => { if (s === 'providers') throw new Error('injected'); };
    try { require('@staffops/otel-helper').setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false }); } catch {}
    console.log(JSON.stringify(lines.filter((l) => l.includes('rollback'))));
  `);
  const [line] = lastJson(res);
  assert.match(line, /^otelhelper: setup\(\) rollback: a step could not be undone error=/);
  for (const secret of ['user', 'pw@', 'token=abc', 'xyz']) assert.ok(!line.includes(secret), `${secret} in ${line}`);
});

test('M2 in a worker thread setup() starts no /metrics listener and says so once', () => {
  const res = runCjs(`
    const { Worker } = require('node:worker_threads');
    const w = new Worker(\`
      const lines = [];
      require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
      const h = require('@staffops/otel-helper');
      h.setup({ serviceName: 'w', environment: 'DEV', metricExporters: ['prometheus'], prometheusMetricsPort: 9 });
      const s = require('@staffops/otel-helper/testing').getState();
      require('node:worker_threads').parentPort.postMessage({ listener: s.runtime.active.listener !== undefined, lines: lines.filter((l) => l.includes('worker thread')) });
    \`, { eval: true });
    w.on('message', (m) => { console.log(JSON.stringify(m)); w.terminate(); });
  `);
  const out = lastJson(res);
  assert.equal(out.listener, false);
  assert.deepEqual(out.lines, ["otelhelper: worker thread: the helper's /metrics listener (OTEL_HELPER_METRICS_PORT=9) is not started outside the main thread; mount metricsHandler() on a server of this thread, or export metrics over OTLP variable=OTEL_HELPER_METRICS_PORT"]);
});

test('L getProviders() is undefined after shutdown and under OTEL_SDK_DISABLED', async () => {
  helper.setup({ serviceName: 'svc' });
  assert.ok(testing.getProviders());
  await helper.shutdown();
  assert.equal(testing.getProviders(), undefined);
  process.env.OTEL_SDK_DISABLED = 'true';
  try {
    helper.setup({ serviceName: 'svc' });
    assert.equal(testing.getProviders(), undefined);
  } finally {
    delete process.env.OTEL_SDK_DISABLED;
  }
});

test('final round: on the shutdown deadline a hung provider stops being the delegate; getResource() is undefined after shutdown', async () => {
  const hanging = { onStart() {}, onEnd() {}, forceFlush: () => Promise.resolve(), shutdown: () => new Promise(() => {}) };
  helper.setup({ serviceName: 'svc', spanProcessors: [hanging] });
  const forwarder = api.trace.getTracerProvider().getDelegate();
  assert.ok(testing.getResource());
  await assert.rejects(helper.shutdown(30), /did not finish within 30 ms/);
  assert.equal(forwarder.getDelegate(), undefined);
  assert.equal(testing.getResource(), undefined);
});
