'use strict';
// @staffops/otel-helper-aws without the AWS SDK (task B10c; spec JS8, design
// D-JS9/D-JS14, contract D14 and A25): options, the result shape, the
// contrib configuration, the attribute and error rules, the D14 load errors
// and library detection, the lifecycle through setup(). This file never loads
// an @aws-sdk client (sdk.test.cjs does, in its own process).
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
const { awsInstrumentation } = require('@staffops/otel-helper-aws');
const attributes = require('../dist/internal/attributes.js');
const internal = require('../dist/internal/aws-instrumentation.js');

const ENTRY = require.resolve('@staffops/otel-helper-aws');
const PKG_DIR = path.resolve(__dirname, '..');
const CONTRIB = '@opentelemetry/instrumentation-aws-sdk';

// One instance for the whole file, as an app creates it.
const instrumentation = awsInstrumentation();

let saved;
let records;
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

/** Runs `source` (CommonJS) as `<dir>/main.cjs` with cwd `dir`; resolves the JSON it prints last, stderr and status. */
function runIn(dir, source, env = {}) {
  const script = path.join(dir, 'main.cjs');
  fs.writeFileSync(script, source);
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [script], {
      cwd: dir,
      env: { ...process.env, OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=DEV', OTEL_HELPER_METRICS_PORT: '0', ...env },
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += d));
    child.stderr.on('data', (d) => (stderr += d));
    child.on('close', (status) => {
      try {
        resolve({ ...JSON.parse(stdout.trim().split('\n').pop()), stderr, status });
      } catch {
        reject(new Error(`child status=${status}\nstdout:\n${stdout}\nstderr:\n${stderr}`));
      }
    });
  });
}

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'otel-aws-'));
}

// ── options and shape ─────────────────────────────────────────────────

test('invalid options throw a TypeError naming the problem (R8); there are no options yet', () => {
  class Opts {}
  for (const [value, message] of [
    [null, /options must be an object, got null/],
    [[], /options must be an object, got array/],
    ['aws', /options must be an object, got string/],
    [new Opts(), /options must be an object, got object/],
    [{ s3: true }, /unknown key\(s\) s3/],
    [{ b: 1, a: 2 }, /unknown key\(s\) a, b/],
  ]) {
    assert.throws(() => awsInstrumentation(value), { name: 'TypeError', message }, JSON.stringify(value));
  }
  const opts = Object.create(null);
  opts.later = undefined;
  assert.deepEqual([...awsInstrumentation(opts).libraries], ['aws-sdk'], 'undefined values count as not set; a null-prototype object is accepted');
  assert.deepEqual([...awsInstrumentation({}).libraries], ['aws-sdk']);
});

test('the result is one Instrumentation named after the package, covering the contrib name (M2)', () => {
  assert.equal(instrumentation.instrumentationName, '@staffops/otel-helper-aws');
  assert.equal(instrumentation.instrumentationVersion, require('../package.json').version);
  assert.deepEqual([...instrumentation.libraries], ['aws-sdk']);
  assert.deepEqual([...instrumentation.coveredInstrumentations], [CONTRIB]);
  const [child] = internal.childrenForTests(instrumentation);
  assert.equal(child.instrumentationName, CONTRIB);
  assert.deepEqual(instrumentation.getConfig(), { enabled: false });
  assert.deepEqual(internal.childrenForTests({}), [], 'only results of awsInstrumentation()');
});

test('the contrib configuration: HTTP under the SDK span suppressed, no statement serializer, no payload parsing, the error hook', () => {
  const [child] = internal.childrenForTests(instrumentation);
  const config = child.getConfig();
  assert.equal(config.suppressInternalInstrumentation, true);
  assert.equal(config.sqsExtractContextPropagationFromPayload, false);
  assert.equal(config.dynamoDBStatementSerializer, undefined, 'no db.query.text (it would serialize the request input)');
  assert.equal(config.preRequestHook, undefined);
  assert.equal(config.exceptionHook, internal.onException);
  assert.equal(config.responseHook, undefined, 'no response hook (a streamed body keeps its HTTP context: D-JS14, B12)');
});

test('setConfig is ignored (enabled is the real state) and warned once per process, on the next enable', () => {
  instrumentation.setConfig({ enabled: true });
  assert.deepEqual(instrumentation.getConfig(), { enabled: false });
  helper.setup({ instrumentations: [instrumentation] });
  instrumentation.setConfig({ enabled: false });
  assert.deepEqual(instrumentation.getConfig(), { enabled: true });
  assert.equal(records.filter((l) => l.includes('@staffops/otel-helper-aws: setConfig() is ignored')).length, 1, records.join('\n'));
});

test('M2: a raw AwsInstrumentation next to awsInstrumentation() is refused by setup(), as is a second result', () => {
  const { AwsInstrumentation } = require(CONTRIB);
  const raw = new AwsInstrumentation({ enabled: false });
  assert.throws(() => helper.setup({ instrumentations: [instrumentation, raw] }), { name: 'TypeError', message: /two instrumentations for "@opentelemetry\/instrumentation-aws-sdk"/ });
  assert.throws(() => helper.setup({ instrumentations: [instrumentation, awsInstrumentation()] }), { message: /two instrumentations for "@staffops\/otel-helper-aws"/ });
});

// ── attribute and error rules ─────────────────────────────────────────

test('sanitizeAttributes: item collection metrics dropped, semconv aliases added, an SNS phone number redacted; idempotent', () => {
  const { sanitizeAttributes, REDACTED_PHONE } = attributes;
  const plain = { 'rpc.service': 'S3', 'cloud.region': undefined };
  assert.equal(sanitizeAttributes(plain), plain, 'nothing to change: the same object');
  assert.deepEqual(sanitizeAttributes({ 'aws.dynamodb.item_collection_metrics': ['{"ItemCollectionKey":"k"}'], 'aws.dynamodb.consumed_capacity': ['{}'] }), { 'aws.dynamodb.consumed_capacity': ['{}'] });
  assert.deepEqual(sanitizeAttributes({ 'aws.request.id': 'r1' }), { 'aws.request.id': 'r1', 'aws.request_id': 'r1' });
  assert.deepEqual(sanitizeAttributes({ 'cloud.region': 'eu-west-1' }), { 'cloud.region': 'eu-west-1', 'aws.region': 'eu-west-1' });
  assert.deepEqual(sanitizeAttributes({ 'aws.request.extended_id': 'h1' }), { 'aws.request.extended_id': 'h1', 'aws.extended_request_id': 'h1' });
  assert.deepEqual(sanitizeAttributes({ 'cloud.region': 'eu-west-1', 'aws.region': 'own' }), { 'cloud.region': 'eu-west-1', 'aws.region': 'own' }, 'a set twin is kept');
  const phone = { 'messaging.system': 'aws.sns', 'messaging.destination': '+15555550123', 'messaging.destination.name': '+15555550123' };
  const redacted = sanitizeAttributes(phone);
  assert.deepEqual(redacted, { 'messaging.system': 'aws.sns', 'messaging.destination': REDACTED_PHONE, 'messaging.destination.name': REDACTED_PHONE });
  assert.equal(phone['messaging.destination'], '+15555550123', 'the input is not mutated');
  assert.deepEqual(sanitizeAttributes(redacted), redacted, 'idempotent');
  assert.equal(REDACTED_PHONE, 'phone_number:**', 'as Python botocore');
  for (const kept of [
    { 'messaging.system': 'aws.sns', 'messaging.destination': 'alerts', 'messaging.destination.name': 'arn:aws:sns:us-east-1:123456789012:alerts' },
    { 'messaging.system': 'aws.sns', 'messaging.destination': 'unknown', 'messaging.destination.name': 'unknown' },
    { 'messaging.system': 'aws_sqs', 'messaging.destination.name': 'orders' },
    { 'messaging.system': 'aws.sns', 'messaging.destination.name': 5 },
  ]) {
    assert.equal(sanitizeAttributes(kept), kept, JSON.stringify(kept));
  }
  assert.deepEqual(sanitizeAttributes({ 'messaging.system': 'aws.sns', 'messaging.destination.name': '+1555' }), { 'messaging.system': 'aws.sns', 'messaging.destination.name': REDACTED_PHONE }, 'only the keys present');
  // L4: the rule needs messaging.system in the same batch (the contrib sets both at span start).
  const alone = { 'messaging.destination.name': '+1555' };
  assert.equal(sanitizeAttributes(alone), alone, 'a destination set alone, without messaging.system, is not recognized');
});

test('awsErrorType: the PascalCase AWS error code from name, else Code; anything else _OTHER (D-JS14)', () => {
  const { awsErrorType } = attributes;
  for (const [err, want] of [
    [{ name: 'NoSuchKey' }, 'NoSuchKey'],
    [{ name: 'ThrottlingException', Code: 'Throttling' }, 'ThrottlingException'],
    [{ name: 'not-a-code', Code: 'AccessDenied' }, 'AccessDenied'],
    [{ name: 'AWS.SimpleQueueService.NonExistentQueue' }, '_OTHER'],
    [{ name: 'lowercase' }, '_OTHER'],
    [{ name: 'A' }, '_OTHER'],
    [{ name: `A${'b'.repeat(63)}` }, `A${'b'.repeat(63)}`],
    [{ name: `A${'b'.repeat(64)}` }, '_OTHER'],
    [{ name: 'Has Space' }, '_OTHER'],
    [{ name: 5, Code: null }, '_OTHER'],
    [new TypeError('x'), 'TypeError'],
    [Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:1'), { code: 'ECONNREFUSED' }), 'ECONNREFUSED'],
    [Object.assign(new Error('x'), { code: 'ERR_SOCKET_CLOSED' }), 'Error'],
    [new Error('x'), 'Error'],
    [{ name: '403' }, 'Forbidden'],
    [{ name: 'Unknown', $metadata: { httpStatusCode: 403 } }, 'Forbidden'],
    [{ name: 'Unknown', $metadata: { httpStatusCode: 503 } }, 'ServiceUnavailable'],
    [{ name: 'Unknown', $metadata: { httpStatusCode: 418 } }, 'Unknown'],
    [{ name: 'Unknown', $metadata: { httpStatusCode: '403' } }, 'Unknown'],
    [{ name: 'Unknown' }, 'Unknown'],
    [{ name: '503', Code: 'x' }, 'ServiceUnavailable'],
    [{ name: '418' }, '_OTHER'],
    [{ name: '599' }, '_OTHER'],
    [Object.assign(new TypeError('x'), { code: 'ECONNRESET' }), 'TypeError'],
    [null, '_OTHER'],
    ['NoSuchKey', '_OTHER'],
    [undefined, '_OTHER'],
  ]) {
    assert.equal(awsErrorType(err), want, JSON.stringify(err));
  }
  assert.ok(attributes.AWS_ERROR_CODE.test('NoSuchKey'));
});

test('errorRequestId: $metadata.requestId when a non-empty string', () => {
  const { errorRequestId } = attributes;
  assert.equal(errorRequestId({ $metadata: { requestId: 'r-1' } }), 'r-1');
  for (const err of [{ $metadata: { requestId: '' } }, { $metadata: { requestId: 5 } }, { $metadata: null }, { $metadata: 'x' }, {}, null, 'x']) {
    assert.equal(errorRequestId(err), undefined, JSON.stringify(err));
  }
});

test('onException: error.type from the error, and the request id when the contrib found none', () => {
  const span = (attrs = {}) => {
    const set = {};
    return { attributes: attrs, set, setAttribute: (k, v) => (set[k] = v) };
  };
  const a = span();
  internal.onException(a, {}, { name: 'NoSuchKey', $metadata: { requestId: 'r-1' } });
  assert.deepEqual(a.set, { 'error.type': 'NoSuchKey', 'aws.request_id': 'r-1' });
  const b = span({ 'aws.request_id': 'from-contrib' });
  internal.onException(b, {}, { name: 'x', $metadata: { requestId: 'r-2' } });
  assert.deepEqual(b.set, { 'error.type': '_OTHER' });
  const c = { setAttribute: (k, v) => (c[k] = v) }; // a span without readable attributes
  internal.onException(c, {}, { $metadata: { requestId: 'r-3' } });
  assert.equal(c['aws.request_id'], 'r-3');
});

test('the tracer binding: ERROR without description, error.type, the legacy db.system and the attribute rules', () => {
  const exporter = new InMemorySpanExporter();
  const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  const inst = awsInstrumentation();
  const [child] = internal.childrenForTests(inst);
  inst.setTracerProvider(provider);
  const span = child.tracer.startSpan('DynamoDB.PutItem', { attributes: { 'db.system.name': 'dynamodb', 'aws.request.id': 'r-1' } });
  span.setAttribute('aws.dynamodb.item_collection_metrics', ['{"ItemCollectionKey":{"pk":{"S":"secret"}}}']);
  span.setStatus({ code: 2, message: 'One or more parameter values were invalid: secret' });
  internal.onException(span, {}, { name: 'ValidationException' });
  span.end();
  const [done] = exporter.getFinishedSpans();
  assert.deepEqual(done.status, { code: 2 });
  assert.deepEqual(done.attributes, { 'db.system.name': 'dynamodb', 'db.system': 'dynamodb', 'aws.request.id': 'r-1', 'aws.request_id': 'r-1', 'error.type': 'ValidationException' });
});

test('the same TracerProvider again is not re-bound; the MeterProvider is bound as is (Bedrock metrics only)', () => {
  const inst = awsInstrumentation();
  const [child] = internal.childrenForTests(inst);
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
    inst.setLoggerProvider('logger');
    assert.deepEqual(calls.map(([k]) => k), ['t', 'm', 'l']);
    assert.notEqual(calls[0][1], tp, 'the tracer is wrapped');
    assert.equal(calls[1][1], mp, 'the meter is not');
  } finally {
    Object.assign(child, { setTracerProvider: original.t, setMeterProvider: original.m, setLoggerProvider: original.l });
  }
});

// ── lifecycle ─────────────────────────────────────────────────────────

test('setup() binds and enables it, shutdown() disables it; every result shares the contrib instrumentation', async () => {
  const [child] = internal.childrenForTests(instrumentation);
  assert.equal(child.isEnabled(), false, 'disabled until setup()');
  helper.setup({ instrumentations: [instrumentation] });
  assert.equal(instrumentation.getConfig().enabled, true);
  assert.equal(child.isEnabled(), true);
  const other = awsInstrumentation();
  assert.equal(internal.childrenForTests(other)[0], child, 'shared');
  other.enable();
  await helper.shutdown();
  assert.equal(instrumentation.getConfig().enabled, false);
  assert.equal(child.isEnabled(), true, 'still enabled by the other result');
  other.disable();
  assert.equal(child.isEnabled(), false);
});

test('OTEL_SDK_DISABLED=true leaves it disabled (no instrumentation, as Python)', () => {
  process.env.OTEL_SDK_DISABLED = 'true';
  helper.setup({ instrumentations: [instrumentation] });
  assert.equal(instrumentation.getConfig().enabled, false);
  assert.equal(internal.childrenForTests(instrumentation)[0].isEnabled(), false);
});

test('awsInstrumentation() does not load the AWS SDK', () => {
  awsInstrumentation();
  assert.deepEqual(Object.keys(require.cache).filter((f) => /[\\/]node_modules[\\/](@aws-sdk|@smithy)[\\/]/.test(f)), []);
  assert.deepEqual([...internal.PACKAGES['aws-sdk']], ['@smithy/core', '@smithy/smithy-client', '@aws-sdk/smithy-client']);
});

// ── library detection and D14 load errors (as -sql R3) ────────────────

test('R3: an SDK that does not resolve from the app is still instrumented; diag debug only (default mode)', async () => {
  const dir = tempDir();
  try {
    const src = `
      const helper = require(${JSON.stringify(require.resolve('@staffops/otel-helper'))});
      const inst = require(${JSON.stringify(ENTRY)}).awsInstrumentation();
      helper.setup({ instrumentations: [inst] });
      console.log(JSON.stringify({ libraries: inst.libraries }));`;
    const lines = (out) => out.stderr.split('\n').filter((l) => l.includes('a library could not be resolved') && l.includes(' library=aws-sdk '));
    const quiet = await runIn(dir, src);
    assert.deepEqual(quiet.libraries, ['aws-sdk']);
    assert.deepEqual(lines(quiet), [], 'default mode: no WARN for an absent SDK');
    const debug = await runIn(dir, src, { OTEL_LOG_LEVEL: 'DEBUG' });
    assert.equal(lines(debug).length, 1, debug.stderr);
    assert.match(lines(debug)[0], /@staffops\/otel-helper-aws: a library could not be resolved from the app directories; instrumented if it loads library=aws-sdk dirs=/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/** Child source that makes the contrib module throw `err` on load (a require.cache entry with a throwing getter). */
function poison(err) {
  return `{ const f = require.resolve(${JSON.stringify(CONTRIB)}, { paths: [${JSON.stringify(PKG_DIR)}] });
      const m = new (require('node:module'))(f); m.loaded = true; m.filename = f;
      Object.defineProperty(m, 'exports', { get() { throw ${err}; } });
      require.cache[f] = m; }`;
}

test('JS8 D14: a contrib that does not load throws: "not installed" when the module itself is missing, else the error verbatim', async () => {
  const dir = tempDir();
  try {
    const attempt = (err) =>
      runIn(dir, `${poison(err)}
        try { require(${JSON.stringify(ENTRY)}).awsInstrumentation(); console.log('{}'); }
        catch (e) { console.log(JSON.stringify({ message: e.message, cause: e.cause && e.cause.message })); }`);
    const boom = await attempt("new Error('contrib boom\\nRequire stack:\\n- /x')");
    assert.deepEqual({ message: boom.message, cause: boom.cause }, { message: 'failed to import the AWS instrumentation: contrib boom', cause: 'contrib boom\nRequire stack:\n- /x' });
    const missing = await attempt(`Object.assign(new Error("Cannot find module '${CONTRIB}'"), { code: 'MODULE_NOT_FOUND' })`);
    assert.equal(missing.message, `AWS instrumentation not installed: ${CONTRIB} is missing. Reinstall @staffops/otel-helper-aws (it depends on ${CONTRIB}).`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
