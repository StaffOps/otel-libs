'use strict';
// OTLP exporter construction and the settings the helper owns (task B4b):
// spec JS2 (helper-owned resolution, env masking, BSP/BLRP/limits), contract
// R2, R8 and the design D1 rows on headers, timeout, compression and the
// effective export deadline; the A18 rulings (D1 rows dated 2026-10-29: TLS
// files, export deadline, code timeout bound, header bytes, value-length
// limits); Node design D-JS3 (ownership table, gRPC channel credentials) and
// D-JS14 (IP-literal gRPC TLS). What reaches the wire is
// asserted by the parity fixture (export cases); the local servers below bind
// 127.0.0.1 on a random port, never the fake receiver's addresses.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const api = require('@opentelemetry/api');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { parseHeaders, resolveHeaders, resolveTimeout, resolveCompression } = require('../dist/internal/otlp-settings.js');
const { resolveSdkSettings } = require('../dist/internal/sdk-settings.js');
const { grpcCredentials, maskedVariables, tlsMaterial, withMaskedEnv } = require('../dist/internal/exporters.js');
const { tryBuild } = require('../dist/internal/setup.js');
const { runCjs, lastJson } = require('./helpers.cjs');

let records;
let saved;
let tmp;

function resetAndCapture() {
  testing.reset();
  records = [];
  testing.setDiagnosticSink((line, meta) => records.push({ line, ...meta }));
}

test.before(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'otelhelper-b4b-'));
});

test.after(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

test.beforeEach(() => {
  saved = { ...process.env };
  for (const k of Object.keys(process.env)) {
    if (k.startsWith('OTEL_') || k === 'SERVICE_NAME' || k === 'ENVIRONMENT') delete process.env[k];
  }
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  resetAndCapture();
});

test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

const lines = (filter = () => true) => records.filter(filter).map((r) => r.line);
const warnings = () => lines((r) => r.level === 'warn');

/** A collecting Warn callback. */
function collector() {
  const out = [];
  const warn = (message, attrs) => out.push({ message, ...attrs });
  return { out, warn };
}

function tmpFile(name, content) {
  const file = path.join(tmp, name);
  fs.writeFileSync(file, content);
  return file;
}

// ── headers ─────────────────────────────────────────────────────────────

test('D1 header parsing: key grammar, lowercase + last wins, whole-value decoding, printable ASCII only (A18)', () => {
  const p = parseHeaders(' A=1 ,x-b=%20two%20,a=3,bad key=x,noeq,%41=y,x-e=a%zzb,x-u=%FF,x-c=x%0D%0Ay,x.d_e-f=ok,=v,x-n=%C3%A9,x-r=\u00e9');
  assert.deepEqual({ ...p.headers }, { a: '3', 'x-b': 'two', 'x-e': 'a%zzb', 'x-u': '%FF', 'x.d_e-f': 'ok' });
  assert.deepEqual({ ...p.positions }, { a: 3, 'x-b': 2, 'x-e': 7, 'x-u': 8, 'x.d_e-f': 10 });
  assert.equal(Object.getPrototypeOf(p.headers), null);
  assert.deepEqual(p.skipped, [4, 5, 6, 9, 11, 12, 13]);
  assert.deepEqual(p.literal, [7, 8]);
  // A literal value that is skipped (not printable ASCII) is only "skipped".
  assert.deepEqual(parseHeaders('k=%zz\u0001').literal, []);
  assert.deepEqual(parseHeaders('k=%zz\u0001').skipped, [1]);
  // A value may contain '='; DEL is not printable.
  assert.deepEqual({ ...parseHeaders('k=a=b').headers }, { k: 'a=b' });
  assert.deepEqual(parseHeaders('k=%7F').skipped, [1]);
});

test('R8 header warnings name the variable and the item position only; per-signal replaces generic, empty falls back', () => {
  const { out, warn } = collector();
  process.env.OTEL_EXPORTER_OTLP_HEADERS = 'authorization=s3cret-generic';
  process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS = 'x-t=1,s3cret-broken';
  const h = (signal, w = () => {}) => {
    const r = resolveHeaders(signal, w);
    return r === undefined ? undefined : [r.variable, { ...r.values }];
  };
  assert.deepEqual({ ...resolveHeaders('traces', () => {}).positions }, { 'x-t': 1 });
  assert.deepEqual(h('traces', warn), ['OTEL_EXPORTER_OTLP_TRACES_HEADERS', { 'x-t': '1' }]);
  assert.deepEqual(h('metrics', warn), ['OTEL_EXPORTER_OTLP_HEADERS', { authorization: 's3cret-generic' }]);
  assert.deepEqual(out, [{ message: 'malformed header item skipped', variable: 'OTEL_EXPORTER_OTLP_TRACES_HEADERS', value: 'item #2', using: 'remaining headers' }]);
  assert.ok(!JSON.stringify(out).includes('s3cret'));
  // Per-signal with no valid item: the generic one is used.
  process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS = 'broken';
  assert.deepEqual(h('traces'), ['OTEL_EXPORTER_OTLP_HEADERS', { authorization: 's3cret-generic' }]);
  // Nothing valid anywhere: an empty object (the SDK must not parse them), unset: undefined.
  process.env.OTEL_EXPORTER_OTLP_HEADERS = 'Authorization Bearer s3cret';
  assert.deepEqual(h('traces'), ['OTEL_EXPORTER_OTLP_TRACES_HEADERS', {}]);
  delete process.env.OTEL_EXPORTER_OTLP_HEADERS;
  delete process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS;
  assert.equal(h('traces'), undefined);
  process.env.OTEL_EXPORTER_OTLP_HEADERS = ' \t';
  assert.equal(h('traces'), undefined);
  // Invalid escapes: warned by position, value kept literally.
  const lit = collector();
  process.env.OTEL_EXPORTER_OTLP_HEADERS = 'x-l=a%20b%zz';
  assert.deepEqual(h('logs', lit.warn), ['OTEL_EXPORTER_OTLP_HEADERS', { 'x-l': 'a%20b%zz' }]);
  assert.deepEqual(lit.out.map((w) => [w.message, w.value]), [['invalid percent-escape in header value; kept literally', 'item #1']]);
});

// ── timeout ─────────────────────────────────────────────────────────────

test('D1 timeout: code > per-signal > generic > 10000; plain decimal ms; garbage falls back; [1 ms, 24 h] clamp', () => {
  const t = (env, code) => {
    for (const k of ['OTEL_EXPORTER_OTLP_TIMEOUT', 'OTEL_EXPORTER_OTLP_TRACES_TIMEOUT']) delete process.env[k];
    Object.assign(process.env, env);
    const { out, warn } = collector();
    const r = resolveTimeout('traces', code, warn);
    return [r.ms, r.source, out.map((w) => `${w.message}|${w.variable}|${w.using}`)];
  };
  assert.deepEqual(t({}), [10000, 'default', []]);
  assert.deepEqual(t({ OTEL_EXPORTER_OTLP_TIMEOUT: ' 500\n' }), [500, 'OTEL_EXPORTER_OTLP_TIMEOUT', []]);
  assert.deepEqual(t({ OTEL_EXPORTER_OTLP_TIMEOUT: '500', OTEL_EXPORTER_OTLP_TRACES_TIMEOUT: '250.5' }), [250.5, 'OTEL_EXPORTER_OTLP_TRACES_TIMEOUT', []]);
  assert.deepEqual(t({ OTEL_EXPORTER_OTLP_TIMEOUT: '500', OTEL_EXPORTER_OTLP_TRACES_TIMEOUT: 'abc' }), [
    500,
    'OTEL_EXPORTER_OTLP_TIMEOUT',
    ['invalid timeout (positive decimal milliseconds)|OTEL_EXPORTER_OTLP_TRACES_TIMEOUT|next level, else 10000'],
  ]);
  for (const bad of ['0', '-5', '+5', '1e3', '0x10', '.5', '5.', '1_000', 'Infinity', 'NaN', '9'.repeat(400)]) {
    const [ms, source, warns] = t({ OTEL_EXPORTER_OTLP_TIMEOUT: bad });
    assert.deepEqual([ms, source, warns.length], [10000, 'default', 1], bad.slice(0, 10));
  }
  assert.deepEqual(t({ OTEL_EXPORTER_OTLP_TIMEOUT: '999999999999' }), [86400000, 'OTEL_EXPORTER_OTLP_TIMEOUT', ['timeout above 24 h; clamped|OTEL_EXPORTER_OTLP_TIMEOUT|86400000']]);
  assert.deepEqual(t({ OTEL_EXPORTER_OTLP_TIMEOUT: '0.5' }), [1, 'OTEL_EXPORTER_OTLP_TIMEOUT', ['timeout below 1 ms; clamped|OTEL_EXPORTER_OTLP_TIMEOUT|1']]);
  // Code wins and the env is not parsed (as in Go).
  assert.deepEqual(t({ OTEL_EXPORTER_OTLP_TIMEOUT: 'abc' }, 1234.5), [1234.5, 'exportTimeoutMs', []]);
});

// ── compression ─────────────────────────────────────────────────────────

test('D1.d compression: code > per-signal > generic > gzip; case-insensitive; unknown warns and falls back', () => {
  const c = (env, code) => {
    for (const k of ['OTEL_EXPORTER_OTLP_COMPRESSION', 'OTEL_EXPORTER_OTLP_LOGS_COMPRESSION']) delete process.env[k];
    Object.assign(process.env, env);
    const { out, warn } = collector();
    return [resolveCompression('logs', code, warn), out.map((w) => `${w.variable}=${w.value}`)];
  };
  assert.deepEqual(c({}), ['gzip', []]);
  assert.deepEqual(c({ OTEL_EXPORTER_OTLP_COMPRESSION: ' NONE ' }), ['none', []]);
  assert.deepEqual(c({ OTEL_EXPORTER_OTLP_COMPRESSION: 'none', OTEL_EXPORTER_OTLP_LOGS_COMPRESSION: 'GZip' }), ['gzip', []]);
  assert.deepEqual(c({ OTEL_EXPORTER_OTLP_COMPRESSION: 'none', OTEL_EXPORTER_OTLP_LOGS_COMPRESSION: 'deflate' }), ['none', ['OTEL_EXPORTER_OTLP_LOGS_COMPRESSION=deflate']]);
  assert.deepEqual(c({ OTEL_EXPORTER_OTLP_COMPRESSION: 'zstd' }), ['gzip', ['OTEL_EXPORTER_OTLP_COMPRESSION=zstd']]);
  assert.deepEqual(c({ OTEL_EXPORTER_OTLP_COMPRESSION: 'gzip' }, ' None '), ['none', []]);
  assert.deepEqual(c({ OTEL_EXPORTER_OTLP_COMPRESSION: 'none' }, ''), ['none', []]);
});

test('R8 code exportTimeoutMs / compression fail fast naming the option; Resolved.otlp carries the effective values', () => {
  const endpoint = { otelEndpoint: 'http://collector:4317' };
  for (const [options, re] of [
    [{ exportTimeoutMs: 0 }, /exportTimeoutMs must be a number of milliseconds in \[1, 86400000\], got 0/],
    [{ exportTimeoutMs: 0.5 }, /in \[1, 86400000\]/],
    [{ exportTimeoutMs: 86400001 }, /in \[1, 86400000\], got 86400001/],
    [{ exportTimeoutMs: Number.NaN }, /in \[1, 86400000\]/],
    [{ exportTimeoutMs: Infinity }, /in \[1, 86400000\]/],
    [{ exportTimeoutMs: '500' }, /exportTimeoutMs must be a number, got string/],
    [{ compression: 'zstd' }, /compression has unknown value "zstd"; valid values: gzip, none/],
    [{ compression: 1 }, /compression must be a string, got number/],
  ]) {
    assert.throws(() => helper.setup({ ...endpoint, ...options }), (err) => err instanceof TypeError && re.test(err.message), JSON.stringify(options));
  }
  process.env.OTEL_EXPORTER_OTLP_TIMEOUT = '700';
  process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
  const otlp = helper.setup({ ...endpoint, exportTimeoutMs: 2500.5, compression: '' }).otlp;
  for (const signal of ['traces', 'metrics', 'logs']) {
    assert.equal(otlp[signal].timeoutMs, 2500.5);
    assert.equal(otlp[signal].timeoutSource, 'exportTimeoutMs');
    assert.equal(otlp[signal].compression, 'none');
  }
});

// ── BSP / BLRP / limits ─────────────────────────────────────────────────

test('JS2 BSP/BLRP/limits: specification defaults; env read by the helper; garbage warns, durations clamped (A18 rulings)', () => {
  const quiet = () => {};
  const d = resolveSdkSettings(quiet);
  assert.deepEqual({ ...d.bsp }, { scheduledDelayMillis: 5000, maxQueueSize: 2048, maxExportBatchSize: 512 });
  assert.deepEqual({ ...d.blrp }, { scheduledDelayMillis: 1000, maxQueueSize: 2048, maxExportBatchSize: 512 });
  assert.deepEqual({ ...d.spanLimits }, {
    attributeValueLengthLimit: Infinity,
    attributeCountLimit: 128,
    eventCountLimit: 128,
    linkCountLimit: 128,
    attributePerEventCountLimit: 128,
    attributePerLinkCountLimit: 128,
  });
  assert.deepEqual({ ...d.logRecordLimits }, { attributeValueLengthLimit: Infinity, attributeCountLimit: 128 });
  Object.assign(process.env, {
    OTEL_BSP_SCHEDULE_DELAY: '0',
    OTEL_BSP_EXPORT_TIMEOUT: ' 1500 ',
    OTEL_BSP_MAX_QUEUE_SIZE: '10',
    OTEL_BSP_MAX_EXPORT_BATCH_SIZE: '50',
    OTEL_BLRP_SCHEDULE_DELAY: '99999999999',
    OTEL_BLRP_MAX_QUEUE_SIZE: '-1',
    OTEL_BLRP_MAX_EXPORT_BATCH_SIZE: '1e3',
    OTEL_METRIC_EXPORT_TIMEOUT: '5',
    OTEL_ATTRIBUTE_COUNT_LIMIT: '7',
    OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT: '20',
    OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT: '3',
    OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT: '0',
    OTEL_SPAN_EVENT_COUNT_LIMIT: '0',
    OTEL_LINK_ATTRIBUTE_COUNT_LIMIT: 'many',
    OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT: '4',
  });
  const { out, warn } = collector();
  const s = resolveSdkSettings(warn);
  assert.deepEqual({ ...s.bsp }, { scheduledDelayMillis: 0, maxQueueSize: 10, maxExportBatchSize: 10 });
  assert.deepEqual({ ...s.blrp }, { scheduledDelayMillis: 86400000, maxQueueSize: 2048, maxExportBatchSize: 512 });
  assert.deepEqual({ ...s.spanLimits }, {
    attributeValueLengthLimit: 20,
    attributeCountLimit: 3,
    eventCountLimit: 0,
    linkCountLimit: 128,
    attributePerEventCountLimit: 128,
    attributePerLinkCountLimit: 128,
  });
  assert.deepEqual({ ...s.logRecordLimits }, { attributeValueLengthLimit: 4, attributeCountLimit: 7 });
  assert.deepEqual(out.map((w) => `${w.variable}: ${w.message.split(';')[0]}`), [
    'OTEL_BSP_EXPORT_TIMEOUT: not honored: the OTLP export timeout is the export deadline of every signal',
    'OTEL_METRIC_EXPORT_TIMEOUT: not honored: the OTLP export timeout is the export deadline of every signal',
    'OTEL_BSP_MAX_EXPORT_BATCH_SIZE,OTEL_BSP_MAX_QUEUE_SIZE: the export batch size is larger than the queue size',
    'OTEL_BLRP_MAX_QUEUE_SIZE: invalid value (positive integer)',
    'OTEL_BLRP_MAX_EXPORT_BATCH_SIZE: invalid value (positive integer)',
    'OTEL_BLRP_SCHEDULE_DELAY: value above 24 h',
    'OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT: invalid value (positive integer)',
    'OTEL_LINK_ATTRIBUTE_COUNT_LIMIT: invalid value (non-negative integer)',
  ]);
  // A value length of 0 is invalid everywhere (A18): the generic 0 means no limit.
  process.env.OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT = '0';
  delete process.env.OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT;
  const z = resolveSdkSettings(quiet);
  assert.equal(z.spanLimits.attributeValueLengthLimit, Infinity);
  assert.equal(z.logRecordLimits.attributeValueLengthLimit, Infinity);
  // A non-zero signal value is kept when the generic one is 0 (A18 final round).
  process.env.OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT = '20';
  assert.equal(resolveSdkSettings(quiet).spanLimits.attributeValueLengthLimit, 20);
});

test('JS2 span limits from the env reach the installed TracerProvider', () => {
  process.env.OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT = '2';
  process.env.OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT = '3';
  helper.setup();
  const span = api.trace.getTracer('t').startSpan('s', { attributes: { a: 'abcdef', b: 'x', c: 'y' } });
  assert.deepEqual({ ...span.attributes }, { a: 'abc', b: 'x' });
  span.end();
});

// ── env masking ─────────────────────────────────────────────────────────

test('JS2 withMaskedEnv hides every helper-owned OTLP variable (TLS files too, A18) and restores them, also on throw', () => {
  const names = maskedVariables();
  assert.equal(names.length, 36);
  for (const suffix of ['ENDPOINT', 'HEADERS', 'TIMEOUT', 'COMPRESSION', 'PROTOCOL', 'INSECURE', 'CERTIFICATE', 'CLIENT_KEY', 'CLIENT_CERTIFICATE']) {
    assert.ok(names.includes(`OTEL_EXPORTER_OTLP_${suffix}`) && names.includes(`OTEL_EXPORTER_OTLP_LOGS_${suffix}`), suffix);
  }
  const env = {
    OTEL_EXPORTER_OTLP_ENDPOINT: 'http://h:4317',
    OTEL_EXPORTER_OTLP_TRACES_HEADERS: 'authorization=s3cret',
    OTEL_EXPORTER_OTLP_METRICS_TIMEOUT: 'abc',
    OTEL_EXPORTER_OTLP_COMPRESSION: 'zstd',
    OTEL_EXPORTER_OTLP_LOGS_PROTOCOL: 'http/json',
    OTEL_EXPORTER_OTLP_INSECURE: 'yes',
    OTEL_EXPORTER_OTLP_CERTIFICATE: '/ca.pem',
    OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY: '/key.pem',
    OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE: 'delta',
  };
  Object.assign(process.env, env);
  const seen = withMaskedEnv(() => Object.keys(process.env).filter((k) => k.startsWith('OTEL_EXPORTER_OTLP')).sort());
  assert.deepEqual(seen, ['OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE']);
  for (const [k, v] of Object.entries(env)) assert.equal(process.env[k], v, k);
  assert.throws(() => withMaskedEnv(() => {
    assert.equal(process.env.OTEL_EXPORTER_OTLP_ENDPOINT, undefined);
    throw new Error('construction failed');
  }), /construction failed/);
  for (const [k, v] of Object.entries(env)) assert.equal(process.env[k], v, k);
});

test('setup writes its exporter diagnostics after the environment is restored (a custom sink never sees the mask)', () => {
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'https://collector:4317';
  process.env.OTEL_EXPORTER_OTLP_CERTIFICATE = path.join(tmp, 'missing.pem');
  const envAtWarning = [];
  testing.setDiagnosticSink((line) => {
    if (line.includes('TLS file cannot be read')) envAtWarning.push(process.env.OTEL_EXPORTER_OTLP_ENDPOINT);
  });
  helper.setup();
  assert.deepEqual(envAtWarning, ['https://collector:4317']);
});

test('R8 setup: the SDK never sees (nor echoes) a rejected value, and the env is restored afterwards', () => {
  const env = {
    OTEL_EXPORTER_OTLP_ENDPOINT: 'http://collector:4317',
    OTEL_EXPORTER_OTLP_HEADERS: 'x-ok=1,Authorization Bearer s3cret-tok',
    OTEL_EXPORTER_OTLP_TRACES_TIMEOUT: 's3cret-timeout',
    OTEL_EXPORTER_OTLP_COMPRESSION: 's3cret-comp',
    OTEL_LOG_LEVEL: 'all',
  };
  Object.assign(process.env, env);
  helper.setup();
  for (const [k, v] of Object.entries(env)) assert.equal(process.env[k], v, k);
  const all = lines().join('\n');
  assert.ok(!all.includes('s3cret-tok'), all);
  // The helper's own warnings echo only non-secret values (timeout/compression), once each.
  assert.equal(lines((r) => r.origin === 'diag' && r.line.includes('s3cret')).length, 0, all);
});

// ── gRPC credentials ────────────────────────────────────────────────────

const tlsEndpoint = (target = 'collector:4317', source = 'OTEL_EXPORTER_OTLP_ENDPOINT') => ({
  protocol: 'grpc',
  url: `https://${target}`,
  grpcTarget: target,
  insecure: false,
  source,
  ignoredTlsVariables: [],
  timeoutMs: 10000,
  compression: 'gzip',
});

test('D-JS3 gRPC credentials: plaintext → insecure; TLS without files → system roots, no warning', () => {
  const { out, warn } = collector();
  assert.equal(grpcCredentials({ ...tlsEndpoint(), insecure: true }, tlsMaterial('traces', new Map(), warn), warn)._isSecure(), false);
  assert.equal(grpcCredentials(tlsEndpoint(), tlsMaterial('traces', new Map(), warn), warn)._isSecure(), true);
  assert.deepEqual(out, []);
});

test('A18 TLS files: per-signal then generic; unreadable / not PEM falls back; the client pair is all-or-nothing', () => {
  const caPem = process.env.FAKEOTLP_CA && fs.existsSync(process.env.FAKEOTLP_CA) ? fs.readFileSync(process.env.FAKEOTLP_CA) : undefined;
  const material = (env) => {
    for (const k of Object.keys(process.env)) if (/^OTEL_EXPORTER_OTLP_.*(CERTIFICATE|CLIENT_KEY)$/.test(k)) delete process.env[k];
    Object.assign(process.env, env);
    const { out, warn } = collector();
    const m = tlsMaterial('traces', new Map(), warn);
    return [Boolean(m.ca), Boolean(m.key), out.map((w) => `${w.message.split(';')[0]}|${w.variable}|${w.using}`)];
  };
  const missing = path.join(tmp, 'missing-ca.pem');
  const garbage = tmpFile('garbage.pem', 'root:x:0:0\n');
  const armored = tmpFile('armored.pem', '-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydA==\n-----END CERTIFICATE-----\n');
  const key = tmpFile('key.pem', 'not a key');
  assert.deepEqual(material({ OTEL_EXPORTER_OTLP_CERTIFICATE: missing }), [false, false, ['TLS file cannot be read|OTEL_EXPORTER_OTLP_CERTIFICATE|the system roots']]);
  assert.deepEqual(material({ OTEL_EXPORTER_OTLP_CERTIFICATE: garbage }), [false, false, ['TLS CA file holds no PEM certificate|OTEL_EXPORTER_OTLP_CERTIFICATE|the system roots']]);
  // Per-signal unparseable, generic unreadable: both warned, system roots.
  assert.deepEqual(material({ OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE: armored, OTEL_EXPORTER_OTLP_CERTIFICATE: missing }), [false, false, [
    'TLS CA file holds no PEM certificate|OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE|the generic variable',
    'TLS file cannot be read|OTEL_EXPORTER_OTLP_CERTIFICATE|the system roots',
  ]]);
  // Only one of the pair: server authentication.
  assert.deepEqual(material({ OTEL_EXPORTER_OTLP_CLIENT_KEY: key }), [false, false, [
    'client key and client certificate must both be set and readable|OTEL_EXPORTER_OTLP_CLIENT_KEY|server authentication',
  ]]);
  // A per-signal key that cannot be read falls back to the generic key; with no certificate: server auth.
  assert.deepEqual(material({ OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY: path.join(tmp, 'k'), OTEL_EXPORTER_OTLP_CLIENT_KEY: key }), [false, false, [
    'TLS file cannot be read|OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY|the generic variable',
    'client key and client certificate must both be set and readable|OTEL_EXPORTER_OTLP_CLIENT_KEY|server authentication',
  ]]);
  // A pair the TLS layer rejects: server authentication, the CA kept.
  const rejected = material({ OTEL_EXPORTER_OTLP_CLIENT_KEY: key, OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE: key });
  assert.deepEqual(rejected, [false, false, [
    'client key and certificate rejected by TLS (bad PEM, mismatch or encrypted key)|OTEL_EXPORTER_OTLP_CLIENT_KEY,OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE|server authentication',
  ]]);
  if (caPem !== undefined) {
    const ca = tmpFile('ca.pem', caPem);
    assert.deepEqual(material({ OTEL_EXPORTER_OTLP_CERTIFICATE: ca }), [true, false, []]);
    // Per-signal bad, generic good (M2): the generic CA is used.
    assert.deepEqual(material({ OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE: '/dev/null', OTEL_EXPORTER_OTLP_CERTIFICATE: ca }), [true, false, [
      'TLS CA file holds no PEM certificate|OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE|the generic variable',
    ]]);
    // Certificate used as the key (mismatch): CA kept, pair dropped.
    assert.deepEqual(material({ OTEL_EXPORTER_OTLP_CERTIFICATE: ca, OTEL_EXPORTER_OTLP_CLIENT_KEY: ca, OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE: ca }).slice(0, 2), [true, false]);
  }
  // The file content and path are never in a warning (R8).
  const { out, warn } = collector();
  process.env.OTEL_EXPORTER_OTLP_CLIENT_KEY = tmpFile('secret-key.pem', 's3cret-key-material');
  process.env.OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE = process.env.OTEL_EXPORTER_OTLP_CLIENT_KEY;
  grpcCredentials(tlsEndpoint(), tlsMaterial('traces', new Map(), warn), warn);
  assert.ok(!JSON.stringify(out).includes('s3cret') && !JSON.stringify(out).includes(tmp), JSON.stringify(out));
});

test('A18 gRPC credentials: a pair gRPC still rejects retries with the CA only (M1), then the system roots', () => {
  // Both seams are read at call time: node:tls (pair pre-check) and grpc-js (what the exporter base calls).
  const tls = require('node:tls');
  const grpc = require('@grpc/grpc-js');
  const origCtx = tls.createSecureContext;
  const origSsl = grpc.credentials.createSsl;
  const calls = [];
  tls.createSecureContext = () => ({});
  grpc.credentials.createSsl = (...args) => {
    calls.push(args.filter((a) => a !== undefined && a !== null).length);
    if (calls.length <= 2) throw new Error('rejected');
    return origSsl.call(grpc.credentials, ...args);
  };
  try {
    process.env.OTEL_EXPORTER_OTLP_CLIENT_KEY = tmpFile('k2.pem', 'k');
    process.env.OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE = tmpFile('c2.pem', 'c');
    const { out, warn } = collector();
    const c = grpcCredentials(tlsEndpoint(), tlsMaterial('traces', new Map(), warn), warn);
    assert.equal(c._isSecure(), true);
    // Pair (2 buffers), CA only (none set: 0), system roots (0).
    assert.deepEqual(calls, [2, 0, 0]);
    assert.deepEqual(out.map((w) => `${w.message.split(';')[0]}|${w.variable}`), [
      'gRPC rejected the TLS client pair|OTEL_EXPORTER_OTLP_CLIENT_KEY,OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE',
      'gRPC rejected the TLS CA|',
    ]);
  } finally {
    tls.createSecureContext = origCtx;
    grpc.credentials.createSsl = origSsl;
  }
});

test('D-JS3 a generic TLS file is read once per setup and warned once for the three signals', () => {
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'https://collector:4317';
  process.env.OTEL_EXPORTER_OTLP_CERTIFICATE = path.join(tmp, 'missing.pem');
  const reads = [];
  const orig = fs.readFileSync;
  fs.readFileSync = function readFileSync(file, ...rest) {
    if (String(file).endsWith('missing.pem')) reads.push(file);
    return orig.call(this, file, ...rest);
  };
  try {
    helper.setup();
  } finally {
    fs.readFileSync = orig;
  }
  assert.equal(reads.length, 1);
  assert.equal(warnings().filter((l) => l.includes('TLS file cannot be read')).length, 1);
});

test('D-JS14 gRPC TLS to an IP-literal endpoint is warned (Node refuses an IP as TLS server name)', () => {
  for (const target of ['10.0.0.1:4317', '[::1]:4317']) {
    const { out, warn } = collector();
    grpcCredentials(tlsEndpoint(target, 'otelEndpoint'), tlsMaterial('metrics', new Map(), warn), warn);
    assert.deepEqual(out.map((w) => w.variable), ['otelEndpoint'], target);
    assert.match(out[0].message, /IP-literal endpoint/);
  }
  const { out, warn } = collector();
  grpcCredentials({ ...tlsEndpoint('10.0.0.1:4317'), insecure: true }, tlsMaterial('metrics', new Map(), warn), warn);
  grpcCredentials(tlsEndpoint('collector.example.com:4317'), tlsMaterial('metrics', new Map(), warn), warn);
  assert.deepEqual(out, []);
});

// ── export over the wire (local servers, not the fake receiver) ─────────

function httpServer() {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      requests.push({ url: req.url, headers: req.headers, body: Buffer.concat(chunks) });
      res.writeHead(200, { 'content-type': 'application/x-protobuf' });
      res.end();
    });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, requests, port: server.address().port }));
  });
}

function emitAll() {
  api.trace.getTracer('t').startSpan('s').end();
  api.metrics.getMeter('m').createCounter('c').add(1);
  api.logs.getLogger('l').emit({ body: 'b' });
}

test('R1/R2 http/protobuf export: request paths, parsed headers, gzip by default, none when set', async () => {
  const { server, requests, port } = await httpServer();
  try {
    process.env.OTEL_EXPORTER_OTLP_HEADERS = 'x-test=a%20b,X-Test=2';
    process.env.OTEL_EXPORTER_OTLP_LOGS_COMPRESSION = 'none';
    helper.setup({ otelEndpoint: `http://127.0.0.1:${port}/otlp`, otlpProtocol: 'http/protobuf' });
    emitAll();
    await helper.shutdown();
    const byPath = Object.fromEntries(requests.map((r) => [r.url, r]));
    assert.deepEqual(Object.keys(byPath).sort(), ['/otlp/v1/logs', '/otlp/v1/metrics', '/otlp/v1/traces']);
    for (const r of requests) assert.equal(r.headers['x-test'], '2', r.url);
    assert.equal(byPath['/otlp/v1/traces'].headers['content-encoding'], 'gzip');
    assert.ok(zlib.gunzipSync(byPath['/otlp/v1/traces'].body).length > 0);
    assert.equal(byPath['/otlp/v1/logs'].headers['content-encoding'], undefined);
  } finally {
    server.close();
  }
});

test('A18 header bytes: a non-ASCII or malformed item never reaches the wire; the other items do (HTTP)', async () => {
  const { server, requests, port } = await httpServer();
  try {
    process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS = 'x-a=%E2%82%AC,x-b=ok,x-broken';
    helper.setup({ tracesEndpoint: `http://127.0.0.1:${port}/v1/traces`, otlpProtocol: 'http/protobuf' });
    api.trace.getTracer('t').startSpan('s').end();
    await helper.shutdown();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].headers['x-b'], 'ok');
    assert.equal(requests[0].headers['x-a'], undefined);
    assert.ok(!Object.keys(requests[0].headers).some((h) => h.includes('broken')), JSON.stringify(requests[0].headers));
    const skipped = warnings().filter((l) => l.includes('malformed header item skipped'));
    assert.deepEqual(skipped.map((l) => /value="([^"]+)"/.exec(l)[1]), ['item #1', 'item #3']);
    assert.ok(!warnings().join('\n').includes('%E2'));
  } finally {
    server.close();
  }
});

test('R1 gRPC export: target with explicit port, metadata, deadline from the resolved timeout; -bin keys sent as bytes', async () => {
  const grpc = require('@grpc/grpc-js');
  const calls = [];
  const identity = (b) => b;
  const server = new grpc.Server();
  const handler = (call, cb) => {
    calls.push({ path: call.getPath(), metadata: call.metadata.getMap(), deadline: call.getDeadline(), at: Date.now() });
    cb(null, Buffer.alloc(0));
  };
  const method = (p) => ({ path: p, requestStream: false, responseStream: false, requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity });
  server.addService(
    {
      t: method('/opentelemetry.proto.collector.trace.v1.TraceService/Export'),
      m: method('/opentelemetry.proto.collector.metrics.v1.MetricsService/Export'),
      l: method('/opentelemetry.proto.collector.logs.v1.LogsService/Export'),
    },
    { t: handler, m: handler, l: handler },
  );
  const port = await new Promise((resolve, reject) => {
    server.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (err, p) => (err ? reject(err) : resolve(p)));
  });
  try {
    process.env.OTEL_EXPORTER_OTLP_HEADERS = 'x-test=1,x-u=%C3%A9,x-bin=raw';
    process.env.OTEL_EXPORTER_OTLP_TIMEOUT = '4000';
    helper.setup({ otelEndpoint: `http://127.0.0.1:${port}` });
    emitAll();
    await helper.shutdown();
    assert.deepEqual(calls.map((c) => c.path.split('.').at(-1)).sort(), ['LogsService/Export', 'MetricsService/Export', 'TraceService/Export']);
    for (const c of calls) {
      assert.equal(c.metadata['x-test'], '1');
      assert.equal(c.metadata['x-u'], undefined);
      assert.ok(Buffer.isBuffer(c.metadata['x-bin']) && c.metadata['x-bin'].toString() === 'raw');
      // The deadline the server sees is the resolved timeout from when the call started.
      const ms = Number(c.deadline) - c.at;
      assert.ok(ms > 3000 && ms <= 4000, String(ms));
    }
    assert.equal(warnings().filter((l) => l.includes('malformed header item skipped') && l.includes('item #2')).length, 1, warnings().join('\n'));
  } finally {
    server.forceShutdown();
  }
});

/** The processors/reader the helper installed (SDK internals, test only). */
function installed() {
  const tp = api.trace.getTracerProvider().getDelegate();
  const mp = api.metrics.getMeterProvider();
  const lp = api.logs.getLoggerProvider();
  const lpDelegate = typeof lp.getDelegate === 'function' ? lp.getDelegate() : lp;
  return {
    bsp: tp._activeSpanProcessor._spanProcessors[0],
    reader: mp._sharedState.metricCollectors[0]._metricReader,
    blrp: lpDelegate._sharedState.registeredLogRecordProcessors[0],
  };
}

test('A18 export deadline: BSP/BLRP get the resolved OTLP timeout; the reader gets it capped, warned with its source', () => {
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://collector:4317';
  process.env.OTEL_EXPORTER_OTLP_METRICS_TIMEOUT = '60000';
  process.env.OTEL_EXPORTER_OTLP_TRACES_TIMEOUT = '1234';
  process.env.OTEL_BSP_EXPORT_TIMEOUT = '7';
  const resolved = helper.setup();
  const { bsp, reader, blrp } = installed();
  assert.equal(bsp._exportTimeoutMillis, resolved.otlp.traces.timeoutMs);
  assert.equal(bsp._exportTimeoutMillis, 1234);
  assert.equal(blrp._exportTimeoutMillis, resolved.otlp.logs.timeoutMs);
  assert.equal(blrp._exportTimeoutMillis, 10000);
  assert.equal(reader._exportTimeout, 30000);
  const capped = warnings().filter((l) => l.includes('metric export timeout is capped at the export interval'));
  assert.equal(capped.length, 1);
  assert.match(capped[0], /variable=OTEL_EXPORTER_OTLP_METRICS_TIMEOUT value=60000 using=30000/);
  assert.equal(warnings().filter((l) => l.includes('not honored') && l.includes('OTEL_BSP_EXPORT_TIMEOUT')).length, 1);
  // Code wins, and the cap names the option.
  testing.reset();
  resetAndCapture();
  helper.setup({ exportTimeoutMs: 45000 });
  assert.equal(installed().bsp._exportTimeoutMillis, 45000);
  assert.match(warnings().find((l) => l.includes('capped')), /variable=exportTimeoutMs/);
});

test('JS2 providers: metrics and logs globals are registered and released on re-setup; a foreign one is kept', async () => {
  helper.setup();
  const mp = api.metrics.getMeterProvider();
  assert.equal(mp.constructor.name, 'MeterProvider');
  await helper.shutdown();
  helper.setup();
  assert.notEqual(api.metrics.getMeterProvider(), mp);
  await helper.shutdown();
  testing.reset();
  resetAndCapture();
  const foreignMeter = { getMeter: () => api.createNoopMeter() };
  const foreignLogs = { getLogger: () => api.createNoopLogger() };
  api.metrics.setGlobalMeterProvider(foreignMeter);
  api.logs.setGlobalLoggerProvider(foreignLogs);
  try {
    helper.setup();
    assert.equal(api.metrics.getMeterProvider(), foreignMeter);
    assert.deepEqual(warnings().filter((l) => /global (MeterProvider|LoggerProvider) is already registered/.test(l)).length, 2);
  } finally {
    api.metrics.disable();
    api.logs.disable();
  }
});

test('R8 tryBuild: a failing exporter or pipeline step is reported, the signal skipped, a built exporter shut down', async () => {
  const pending = [];
  assert.equal(tryBuild('traces', () => { throw new Error('boom from the SDK'); }, () => 'never', pending), undefined);
  let shut = 0;
  const exporter = { shutdown: async () => { shut++; } };
  assert.equal(tryBuild('logs', () => exporter, () => { throw new TypeError('processor'); }, pending), undefined);
  assert.equal(tryBuild('metrics', () => exporter, (e) => e, pending), exporter);
  await new Promise((r) => setImmediate(r));
  assert.equal(shut, 1);
  assert.deepEqual(pending.map(([m, a]) => `${m}|${a.signal}|${a.error}`), [
    'the OTLP exporter could not be built; this signal is not exported|traces|boom from the SDK',
    'the OTLP exporter could not be built; this signal is not exported|logs|processor',
  ]);
});

test('shutdown() (B4b review): a failing provider rejects with an AggregateError naming it and the first reason', async () => {
  helper.setup();
  const tp = api.trace.getTracerProvider().getDelegate();
  tp.shutdown = () => Promise.reject(new Error('flush failed'));
  await assert.rejects(helper.shutdown(), (err) => err instanceof AggregateError && /TracerProvider failed to shut down \(first: flush failed\)/.test(err.message));
});

test('item 1: requiring the helper does not load @grpc/grpc-js; an http/protobuf setup does not either', () => {
  const res = runCjs(`
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
    const h = require('@staffops/otel-helper');
    const count = () => Object.keys(require.cache).filter((k) => k.includes('grpc-js')).length;
    const before = count();
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://collector:4318';
    h.setup({ serviceName: 's', environment: 'DEV' });
    const http = count();
    console.log(JSON.stringify({ before, http }));
  `);
  assert.deepEqual(lastJson(res), { before: 0, http: 0 });
});
