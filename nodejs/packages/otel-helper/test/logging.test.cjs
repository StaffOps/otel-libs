'use strict';
// createLogger() (task B8; spec JS6, design D-JS6; contract R4 and design D6):
// per record the OTel Logs API while a logs exporter is active, else one JSON
// line on stdout; the level of R4 on both paths.
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const api = require('@opentelemetry/api');
const { LoggerProvider } = require('@opentelemetry/sdk-logs');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { emitRecord, errorText, otelAttributes, stdoutLine } = require('../dist/internal/logging.js');
const { logDestination } = require('../dist/internal/setup.js');
const { runCjs, lastJson } = require('./helpers.cjs');

const VARS = [
  'OTEL_SERVICE_NAME', 'OTEL_RESOURCE_ATTRIBUTES', 'SERVICE_NAME', 'ENVIRONMENT', 'OTEL_HELPER_DEBUG_LEVEL', 'OTEL_SDK_DISABLED',
  'OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', 'OTEL_EXPORTER_OTLP_PROTOCOL', 'OTEL_EXPORTER_OTLP_COMPRESSION',
  'OTEL_LOGS_EXPORTER', 'OTEL_TRACES_EXPORTER', 'OTEL_METRICS_EXPORTER',
];
// Children inherit it: never bind :9464 from unit tests.
process.env.OTEL_HELPER_METRICS_PORT = '0';
const TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

let saved;
let records;

test.beforeEach(() => {
  saved = Object.fromEntries(VARS.map((k) => [k, process.env[k]]));
  for (const k of VARS) delete process.env[k];
  testing.reset();
  records = [];
  testing.setDiagnosticSink((line, meta) => records.push({ line, ...meta }));
});

test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

/** Runs fn with process.stdout.write captured; returns the written JSON lines (parsed). */
function captureStdout(fn) {
  const chunks = [];
  const original = process.stdout.write;
  process.stdout.write = (chunk) => {
    chunks.push(String(chunk));
    return true;
  };
  try {
    fn();
  } finally {
    process.stdout.write = original;
  }
  return chunks.join('').split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

test('JS6 createLogger: options are validated (TypeError naming the option); the logger is frozen', () => {
  for (const [options, message] of [
    [undefined, /^createLogger\(\): options must be an object, got undefined$/],
    [null, /options must be an object, got null/],
    ['x', /options must be an object, got string/],
    [{}, /name must be a non-empty string, got undefined/],
    [{ name: 42 }, /name must be a non-empty string, got number/],
    [{ name: '  ' }, /name must be a non-empty string, got " {2}"/],
    [{ name: 'a', level: 'trace' }, /level must be one of debug, info, warn, error, got "trace"/],
    [{ name: 'a', level: 3 }, /level must be one of debug, info, warn, error, got number/],
  ]) {
    assert.throws(() => helper.createLogger(options), (err) => err instanceof TypeError && message.test(err.message), String(JSON.stringify(options)));
  }
  const logger = helper.createLogger({ name: 'app' });
  assert.equal(logger.name, 'app');
  assert.ok(Object.isFrozen(logger));
  assert.deepEqual(Object.keys(logger).sort(), ['debug', 'error', 'info', 'name', 'warn']);
});

test('D6 stdout fallback before setup(): one JSON line per record, Go/Python field names, level info by default', () => {
  const logger = helper.createLogger({ name: 'app' });
  const out = captureStdout(() => {
    logger.debug('dropped');
    logger.info('hello', { 'http.route': '/x', count: 2, ok: true });
    logger.warn('w');
    logger.error('e');
  });
  assert.equal(out.length, 3);
  assert.deepEqual(Object.keys(out[0]), ['time', 'level', 'msg', 'logger', 'http.route', 'count', 'ok']);
  assert.match(out[0].time, TIME);
  assert.deepEqual({ ...out[0], time: undefined }, { time: undefined, level: 'INFO', msg: 'hello', logger: 'app', 'http.route': '/x', count: 2, ok: true });
  assert.deepEqual(out.map((r) => r.level), ['INFO', 'WARN', 'ERROR']);
  // No trace ids on stdout (as Python), even inside a span.
  assert.equal('trace_id' in out[0] || 'traceId' in out[0], false);
});

test('R4 level: from the environment setup() resolved, read per record, kept after shutdown, explicit level wins', async () => {
  const logger = helper.createLogger({ name: 'app' });
  const fixed = helper.createLogger({ name: 'fixed', level: 'debug' });
  const levels = () => captureStdout(() => {
    for (const l of ['debug', 'info', 'warn', 'error']) logger[l](l);
  }).map((r) => r.msg);
  helper.setup({ serviceName: 's', environment: 'PRD' });
  assert.deepEqual(levels(), ['warn', 'error'], 'PRD = warn');
  await helper.shutdown();
  assert.deepEqual(levels(), ['warn', 'error'], 'kept after shutdown (Go installed level)');
  helper.setup({ serviceName: 's', environment: 'LOCAL' });
  assert.deepEqual(levels(), ['debug', 'info', 'warn', 'error'], 'LOCAL = debug');
  await helper.shutdown();
  helper.setup({ serviceName: 's', environment: 'LOCAL', minimumLogLevel: 'error' });
  assert.deepEqual(levels(), ['error'], 'code minimumLogLevel');
  assert.deepEqual(captureStdout(() => fixed.debug('d')).map((r) => r.msg), ['d'], 'explicit level');
  await helper.shutdown();
  process.env.ENVIRONMENT = 'staging';
  helper.setup({ serviceName: 's' });
  assert.deepEqual(levels(), ['info', 'warn', 'error'], 'unknown environment = info');
  await helper.shutdown();
  process.env.OTEL_HELPER_DEBUG_LEVEL = 'true';
  process.env.ENVIRONMENT = 'PRD';
  helper.setup({ serviceName: 's' });
  assert.deepEqual(levels(), ['debug', 'info', 'warn', 'error'], 'debug wins');
  await helper.shutdown();
  testing.reset();
  assert.deepEqual(levels(), ['info', 'warn', 'error'], 'reset forgets the level');
});

test('D6 stdout: OTEL_LOGS_EXPORTER=none and OTEL_SDK_DISABLED=true write to stdout even with an endpoint', async () => {
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://127.0.0.1:9';
  for (const env of [{ OTEL_LOGS_EXPORTER: 'none' }, { OTEL_SDK_DISABLED: 'true' }]) {
    Object.assign(process.env, env);
    helper.setup({ serviceName: 's', environment: 'DEV' });
    assert.equal(logDestination().provider, undefined, JSON.stringify(env));
    assert.deepEqual(captureStdout(() => helper.createLogger({ name: 'a' }).warn('to-stdout')).map((r) => r.msg), ['to-stdout']);
    await helper.shutdown();
    for (const k of Object.keys(env)) delete process.env[k];
  }
});

test('D6 stdout attributes: reserved keys skipped, first Error renamed to exception, JSON-safe values', () => {
  const err = new Error('boom');
  const circular = { a: 1 };
  circular.self = circular;
  const line = JSON.parse(stdoutLine('app', 'error', 'm', {
    time: 'x', level: 'x', msg: 'x', logger: 'x',
    err,
    second: new TypeError('two'),
    big: 10n,
    sym: Symbol('s'),
    fn: function named() {},
    nested: { b: [1, 'two'] },
    circular,
    bad: { toJSON() { throw new Error('nope'); } },
    nothing: undefined,
    when: new Date(0),
  }, new Date(Date.UTC(2026, 10, 2, 1, 2, 3, 4))));
  assert.equal(line.time, '2026-11-02T01:02:03.004Z');
  assert.deepEqual([line.level, line.msg, line.logger], ['ERROR', 'm', 'app']);
  assert.equal(line.exception, err.stack);
  assert.equal('err' in line, false);
  assert.match(line.second, /^TypeError: two\n/);
  assert.equal(line.big, '10');
  assert.equal(line.sym, 'Symbol(s)');
  assert.equal(line.fn, '[Function: named]');
  assert.deepEqual(line.nested, { b: [1, 'two'] });
  assert.deepEqual(line.circular, { a: 1, self: '[Circular]' });
  const shared = { v: 1 };
  assert.deepEqual(JSON.parse(stdoutLine('a', 'info', 'm', { pair: { x: shared, y: shared } }, new Date())).pair, { x: { v: 1 }, y: { v: 1 } }, 'a repeated reference is not a cycle');
  assert.equal(line.bad, '[unreadable object]');
  assert.equal(line.nothing, null);
  assert.equal(line.when, '1970-01-01T00:00:00.000Z');
  assert.deepEqual(Object.keys(line).slice(-1), ['exception']);
  // Go: an attribute already named `exception` is kept and no error is renamed.
  const kept = JSON.parse(stdoutLine('app', 'warn', 'm', { exception: 'mine', err }, new Date()));
  assert.equal(kept.exception, 'mine');
  assert.equal(kept.err, err.stack);
  // No attributes, null attributes.
  assert.deepEqual(Object.keys(JSON.parse(stdoutLine('a', 'info', 'm', undefined, new Date()))), ['time', 'level', 'msg', 'logger']);
  assert.deepEqual(Object.keys(JSON.parse(stdoutLine('a', 'info', 'm', null, new Date()))), ['time', 'level', 'msg', 'logger']);
});

test('errorText: stack, else Name: message, never throws', () => {
  const noStack = new Error('m');
  noStack.stack = undefined;
  assert.equal(errorText(noStack), 'Error: m');
  const empty = new RangeError('r');
  empty.stack = '';
  assert.equal(errorText(empty), 'RangeError: r');
  const hostile = new Error('h');
  Object.defineProperty(hostile, 'stack', { get() { throw new Error('x'); } });
  Object.defineProperty(hostile, 'name', { get() { throw new Error('x'); } });
  assert.equal(errorText(hostile), '[object]');
});

test('emitRecord never throws: a hostile message, a throwing stdout', () => {
  const hostile = { toString() { throw new Error('x'); } };
  const out = captureStdout(() => emitRecord('a', undefined, 'warn', hostile));
  assert.deepEqual(out.map((r) => r.msg), ['[object]']);
  assert.deepEqual(captureStdout(() => emitRecord('a', undefined, 'warn', 42)).map((r) => r.msg), ['42']);
  const original = process.stdout.write;
  process.stdout.write = () => {
    throw new Error('EPIPE');
  };
  try {
    assert.doesNotThrow(() => helper.createLogger({ name: 'a' }).error('x'));
    assert.doesNotThrow(() => helper.createLogger({ name: 'a' }).error('y'));
  } finally {
    process.stdout.write = original;
  }
  // Reported once on the diagnostics sink, the reason only (R8: no values).
  const failed = records.filter((r) => r.line.includes('a helper log record could not be written'));
  assert.deepEqual(failed.map((r) => r.line), ['otelhelper: a helper log record could not be written and was dropped reason=EPIPE']);
});

test('N1 unreadable attributes: a throwing getter or Proxy trap is a marker, never a lost record; an unlistable object is reported', () => {
  const getter = Object.defineProperty({ ok: 1 }, 'bad', { enumerable: true, get() { throw new Error('secret-value'); } });
  const nestedProxy = { inner: new Proxy({}, { ownKeys() { throw new Error('trap'); } }) };
  const arrayProxy = new Proxy([1, 2], { get(t, k) { if (k === '1') throw new Error('idx'); return Reflect.get(t, k); } });
  const line = JSON.parse(stdoutLine('a', 'warn', 'm', { getter, nested: nestedProxy, arr: arrayProxy }, new Date()));
  assert.deepEqual(line.getter, { ok: 1, bad: '[unreadable accessor]' });
  assert.deepEqual(line.nested, { inner: '[unreadable object]' });
  assert.deepEqual(line.arr, [1, '[unreadable accessor]']);
  const top = Object.defineProperty({}, 'bad', { enumerable: true, get() { throw new Error('x'); } });
  assert.equal(JSON.parse(stdoutLine('a', 'warn', 'm', top, new Date())).bad, '[unreadable accessor]');
  const { attrs } = otelAttributes({ getter, nested: nestedProxy, arr: arrayProxy, ...{} });
  assert.deepEqual(attrs.getter, { ok: 1, bad: '[unreadable accessor]' });
  assert.deepEqual(attrs.nested, { inner: '[unreadable object]' });
  assert.deepEqual(attrs.arr, [1, '[unreadable accessor]']);
  assert.equal(otelAttributes(top).attrs.bad, '[unreadable accessor]');
  // An attribute object that cannot be listed: the record is dropped and reported once.
  const unlistable = new Proxy({}, { ownKeys() { throw new Error('no keys'); } });
  assert.throws(() => stdoutLine('a', 'warn', 'm', unlistable, new Date()), /the attributes cannot be listed/);
  assert.throws(() => otelAttributes(unlistable), /the attributes cannot be listed/);
  assert.deepEqual(captureStdout(() => helper.createLogger({ name: 'a' }).warn('lost', unlistable)), []);
  const failed = records.filter((r) => r.line.includes('could not be written'));
  assert.deepEqual(failed.map((r) => r.line), ['otelhelper: a helper log record could not be written and was dropped reason="the attributes cannot be listed"']);
  assert.ok(!records.some((r) => r.line.includes('secret-value')));
});

test('A23 review: a Proxy whose getPrototypeOf throws and a revoked Proxy are one line with [unreadable object]', () => {
  const trap = new Proxy({}, { getPrototypeOf() { throw new Error('proto'); } });
  const { proxy: revoked, revoke } = Proxy.revocable({}, {});
  revoke();
  for (const value of [trap, revoked]) {
    const out = captureStdout(() => helper.createLogger({ name: 'a' }).warn('proxy', { value }));
    assert.equal(out.length, 1);
    assert.equal(out[0].value, '[unreadable object]');
    assert.equal(otelAttributes({ value }).attrs.value, '[unreadable object]');
  }
  assert.ok(!records.some((r) => r.line.includes('could not be written')));
});

test('A23 review: a toJSON returning this lists the own keys', () => {
  const self = { a: 1, toJSON() { return this; } };
  assert.deepEqual(JSON.parse(stdoutLine('a', 'info', 'm', { self }, new Date())).self, { a: 1, toJSON: '[Function: toJSON]' });
});

test('N2 Map and Set: a Map is an object (string keys), a Set an array, on both paths', () => {
  const value = { m: new Map([['a', 1], [2, new Set(['x', 'y'])]]), s: new Set([1, { k: 'v' }]) };
  const line = JSON.parse(stdoutLine('a', 'info', 'm', value, new Date()));
  assert.deepEqual(line.m, { a: 1, 2: ['x', 'y'] });
  assert.deepEqual(line.s, [1, { k: 'v' }]);
  const { attrs } = otelAttributes(value);
  assert.deepEqual(attrs.m, { a: 1, 2: ['x', 'y'] });
  assert.deepEqual(attrs.s, [1, { k: 'v' }]);
  const cyclic = new Map();
  cyclic.set('self', cyclic);
  assert.deepEqual(JSON.parse(stdoutLine('a', 'info', 'm', { cyclic }, new Date())).cyclic, { self: '[Circular]' });
});

test('N4/N5/N6 stdout: functions as [Function: name], a top-level __proto__ attribute kept, an Error under a reserved key renamed', () => {
  const line = JSON.parse(stdoutLine('a', 'info', 'm', { f: () => 1, g: function () {}, h: class K {} }, new Date()));
  assert.deepEqual([line.f, line.g, line.h], ['[Function: f]', '[Function: g]', '[Function: K]']);
  assert.equal(JSON.parse(stdoutLine('a', 'info', 'm', { x: [function () {}] }, new Date())).x[0], '[Function: anonymous]');
  const proto = stdoutLine('a', 'info', 'm', JSON.parse('{"__proto__": "p", "o": {"__proto__": "q"}}'), new Date());
  assert.match(proto, /"__proto__":"p"/);
  assert.match(proto, /"o":\{"__proto__":"q"\}/);
  const err = new Error('under msg');
  const renamed = JSON.parse(stdoutLine('a', 'warn', 'm', { msg: err, other: 1 }, new Date()));
  assert.equal(renamed.msg, 'm');
  assert.equal(renamed.exception, err.stack);
  const kept = JSON.parse(stdoutLine('a', 'warn', 'm', { exception: 'given', level: new Error('x') }, new Date()));
  assert.deepEqual([kept.exception, kept.level], ['given', 'WARN'], 'an exception key wins; the error under a reserved key is dropped');
});

test('otelAttributes: objects as maps and arrays as arrays (as Go and Python), the first Error becomes the record exception', () => {
  const err = new Error('boom');
  const { attrs, exception } = otelAttributes({
    s: 's', n: 1, b: false,
    homogeneous: ['a', null, 'b'],
    mixed: [1, 'a', true, null],
    nested: { order: { id: 'o-1', items: [{ sku: 'x', qty: 2 }] }, tags: ['t1'] },
    err, err2: new Error('two'), big: 1n, when: new Date(0), invalid: new Date(NaN),
    bytes: new Uint8Array([1, 2]), set: new Set([1]), inst: new (class Point { constructor() { this.x = 1; } })(),
    deepErr: { e: new Error('inner') }, nan: NaN, nul: null, undef: undefined,
  });
  assert.equal(exception, err);
  assert.deepEqual({ ...attrs, err2: attrs.err2.split('\n')[0], deepErr: { e: attrs.deepErr.e.split('\n')[0] } }, {
    s: 's', n: 1, b: false,
    homogeneous: ['a', null, 'b'],
    mixed: [1, 'a', true, null],
    nested: { order: { id: 'o-1', items: [{ sku: 'x', qty: 2 }] }, tags: ['t1'] },
    err2: 'Error: two', big: '1', when: '1970-01-01T00:00:00.000Z', invalid: 'Invalid Date',
    bytes: new Uint8Array([1, 2]), set: [1], inst: '{"x":1}',
    deepErr: { e: 'Error: inner' }, nan: NaN,
  });
  // A cycle and a sibling reference; nesting deeper than 100 (Go otelslog's limit and marker).
  const cyclic = { a: 1 };
  cyclic.self = cyclic;
  const shared = { v: 1 };
  let deep = 'leaf';
  for (let i = 0; i < 101; i++) deep = [deep];
  const more = otelAttributes({ cyclic, pair: { x: shared, y: shared }, deep }).attrs;
  assert.deepEqual(more.cyclic, { a: 1, self: '[Circular]' });
  assert.deepEqual(more.pair, { x: { v: 1 }, y: { v: 1 } });
  let probe = more.deep;
  for (let i = 0; i < 100; i++) probe = probe[0];
  assert.equal(probe, '<max-depth-exceeded>');
  const proto = otelAttributes(JSON.parse('{"__proto__": "p", "m": {"__proto__": "q"}}')).attrs;
  assert.equal(Object.getOwnPropertyDescriptor(proto, '__proto__')?.value, 'p');
  assert.equal(Object.getOwnPropertyDescriptor(proto.m, '__proto__')?.value, 'q');
  const nullProto = Object.assign(Object.create(null), { k: 'v' });
  assert.deepEqual({ ...otelAttributes({ m: nullProto }).attrs.m }, { k: 'v' }, 'a null-prototype object is a map');
  // A non-plain object whose JSON text cannot be built: String(value).
  const hostile = new (class Hostile { toJSON() { throw new Error('x'); } })();
  assert.equal(otelAttributes({ hostile }).attrs.hostile, '[unreadable object]');
  assert.deepEqual(otelAttributes(undefined), { attrs: {}, exception: undefined });
  assert.deepEqual(otelAttributes(null), { attrs: {}, exception: undefined });
});

test('D6 stdout: non-finite numbers are text, so the line stays valid JSON (A23)', () => {
  const line = stdoutLine('a', 'warn', 'm', { nan: NaN, inf: Infinity, ninf: -Infinity, ok: 1.5, nested: { a: [NaN] } }, new Date());
  const entry = JSON.parse(line);
  assert.deepEqual([entry.nan, entry.inf, entry.ninf, entry.ok], ['NaN', 'Infinity', '-Infinity', 1.5]);
  assert.deepEqual(entry.nested, { a: ['NaN'] });
  assert.doesNotMatch(line, /null/);
  assert.equal(JSON.parse(stdoutLine('a', 'warn', 'm', { top: NaN }, new Date())).top, 'NaN');
});

// ── the OTel path ────────────────────────────────────────────────────────

function httpServer() {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      requests.push({ url: req.url, body: Buffer.concat(chunks) });
      res.writeHead(200, { 'content-type': 'application/x-protobuf' });
      res.end();
    });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, requests, port: server.address().port }));
  });
}

async function otlpRecords(fn) {
  const { server, requests, port } = await httpServer();
  try {
    process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
    process.env.OTEL_TRACES_EXPORTER = 'none';
    process.env.OTEL_METRICS_EXPORTER = 'none';
    helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: `http://127.0.0.1:${port}`, otlpProtocol: 'http/protobuf' });
    const stdout = captureStdout(fn);
    await helper.shutdown();
    return { stdout, logs: requests.filter((r) => r.url === '/v1/logs').map((r) => r.body.toString('latin1')).join('') };
  } finally {
    server.close();
  }
}

test('D6 OTLP active: records go through the Logs API only (scope = logger name, level applied, trace-correlated), never to stdout', async () => {
  let ids;
  const { stdout, logs } = await otlpRecords(() => {
    const logger = helper.createLogger({ name: 'orders-logger' });
    logger.debug('below-the-level');
    api.trace.getTracer('t').startActiveSpan('sp', (span) => {
      ids = span.spanContext();
      logger.warn('otlp-record', { 'order.id': 'o-1', err: new Error('kaput'), cart: { lineItem: 'sku-9' } });
      logger.warn('otlp-arrays', { mixed: [1, 'a', true], rows: [{ sku: 'x-sku' }] });
      span.end();
    });
  });
  // Trace correlation on the wire: the record carries the active span's ids (raw protobuf bytes).
  assert.ok(logs.includes(Buffer.from(ids.traceId, 'hex').toString('latin1')), 'trace id on the wire');
  assert.ok(logs.includes(Buffer.from(ids.spanId, 'hex').toString('latin1')), 'span id on the wire');
  assert.deepEqual(stdout, []);
  assert.match(logs, /otlp-record/);
  assert.match(logs, /orders-logger/);
  assert.match(logs, /order\.id/);
  // A map on the wire (OTLP kvlist), not JSON text.
  assert.match(logs, /lineItem/);
  assert.doesNotMatch(logs, /\{"lineItem"/);
  // A mixed array and an array of maps arrive decoded (OTLP array/kvlist values), not as JSON text.
  assert.match(logs, /otlp-arrays/);
  assert.match(logs, /x-sku/);
  assert.doesNotMatch(logs, /\[1,"a",true\]/);
  assert.doesNotMatch(logs, /\[\{"sku"/);
  assert.ok(!records.some((r) => /Invalid attribute value/.test(r.line)), records.map((r) => r.line).join('\n'));
  assert.match(logs, /exception\.message[\s\S]*kaput/);
  assert.match(logs, /WARN/);
  assert.doesNotMatch(logs, /below-the-level/);
});

test('A23 review: hostile getters on an Error never run inside the SDK nor reach the report', async () => {
  const hostile = new Error('placeholder');
  Object.defineProperty(hostile, 'message', { get() { throw new Error('secret-token'); } });
  Object.defineProperty(hostile, 'stack', { get() { throw new Error('secret-token'); } });
  const { stdout, logs } = await otlpRecords(() => {
    helper.createLogger({ name: 'h' }).error('hostile-err', { err: hostile });
  });
  assert.deepEqual(stdout, []);
  assert.match(logs, /hostile-err/);
  assert.match(logs, /exception\.type[\s\S]*Error/);
  assert.ok(!records.some((r) => r.line.includes('secret-token') || r.line.includes('could not be written')));
});

test('D6 after shutdown() a logger goes back to stdout; a foreign global LoggerProvider does not stop the helper loggers (Go, Python)', async () => {
  const foreign = new LoggerProvider();
  assert.equal(api.logs.setGlobalLoggerProvider(foreign), true);
  try {
    const logger = helper.createLogger({ name: 'app' });
    const { stdout, logs } = await otlpRecords(() => logger.warn('through-the-helper'));
    assert.deepEqual(stdout, []);
    assert.match(logs, /through-the-helper/);
    assert.ok(records.some((r) => r.line.includes("a global LoggerProvider is already registered; the helper does not replace it: OpenTelemetry Logs API callers use it, the helper's loggers (createLogger) still export through the helper's provider")));
    assert.deepEqual(captureStdout(() => logger.warn('after')).map((r) => r.msg), ['after']);
  } finally {
    api.logs.disable();
  }
});

test('D6 OTEL_LOGS_EXPORTER=console: the SDK console exporter prints the record, no JSON line (child process)', () => {
  const res = runCjs(`
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
    const api = require('@opentelemetry/api');
    const h = require('@staffops/otel-helper');
    h.setup({ serviceName: 's', environment: 'DEV' });
    const logger = h.createLogger({ name: 'console-logger' });
    api.trace.getTracer('t').startActiveSpan('sp', (span) => {
      logger.warn('console-record', { k: 'v' });
      span.end();
    });
    h.shutdown().then(() => console.log(JSON.stringify({ done: true })));
  `, { env: { OTEL_LOGS_EXPORTER: 'console', OTEL_TRACES_EXPORTER: 'none', OTEL_METRICS_EXPORTER: 'none', OTEL_EXPORTER_OTLP_ENDPOINT: '' } });
  assert.deepEqual(lastJson(res), { done: true });
  assert.match(res.stdout, /body: 'console-record'/);
  assert.match(res.stdout, /severityText: 'WARN'/);
  assert.match(res.stdout, /spanId: '[0-9a-f]{16}'/, 'trace-correlated on the OTel path');
  assert.doesNotMatch(res.stdout, /"msg":"console-record"/);
});

test('a registry of another @opentelemetry/api version: the helper loggers write to stdout (child process)', () => {
  const res = runCjs(`
    globalThis[Symbol.for('opentelemetry.js.api.1')] = { version: '1.9.0' };
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
    const h = require('@staffops/otel-helper');
    h.setup({ serviceName: 's', environment: 'DEV' });
    h.createLogger({ name: 'a' }).warn('foreign-api');
  `, { env: { OTEL_LOGS_EXPORTER: 'console', OTEL_EXPORTER_OTLP_ENDPOINT: '' } });
  const line = JSON.parse(res.stdout.trim().split('\n').find((l) => l.startsWith('{"time"')));
  assert.equal(line.msg, 'foreign-api');
});

test('an inactive copy (another helper version owns the process): stdout at info (child process)', () => {
  const res = runCjs(`
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'), { value: { version: '9.9.9', hooks: undefined } });
    const h = require('@staffops/otel-helper');
    h.setup({ serviceName: 's', environment: 'LOCAL' });
    const logger = h.createLogger({ name: 'a' });
    logger.debug('dropped');
    logger.info('inactive-copy');
  `, { env: { OTEL_LOGS_EXPORTER: 'console', OTEL_EXPORTER_OTLP_ENDPOINT: '' } });
  const lines = res.stdout.trim().split('\n').filter((l) => l.startsWith('{"time"')).map((l) => JSON.parse(l).msg);
  assert.deepEqual(lines, ['inactive-copy']);
});

test('the ESM entry exposes the same createLogger()', async () => {
  const esm = await import('@staffops/otel-helper');
  assert.equal(esm.createLogger, helper.createLogger);
});

// ── the internal bridge entry (B10d: -pino / -winston) ──────────────────

const bridge = require('@staffops/otel-helper/internal/bridge');

test('B10d internal/bridge: the entry exports exactly what the bridge packages use', () => {
  assert.deepEqual(Object.keys(bridge).filter((k) => k !== '__esModule').sort(), ['LOG_LEVELS', 'ROOT_CONTEXT', 'activeContext', 'emitBridgeRecord', 'flushBridgeRecords', 'recordEnabled']);
  assert.equal(bridge.ROOT_CONTEXT, api.ROOT_CONTEXT);
  assert.equal(bridge.LOG_LEVELS, require('../dist/internal/log-level.js').LOG_LEVELS);
});

test('B10d recordEnabled: trace never, fatal always, the rest by the fixed or installed level', () => {
  assert.equal(bridge.recordEnabled(undefined, 'trace'), false);
  assert.equal(bridge.recordEnabled('debug', 'trace'), false);
  assert.equal(bridge.recordEnabled('error', 'fatal'), true);
  assert.equal(bridge.recordEnabled(undefined, 'debug'), false);
  assert.equal(bridge.recordEnabled(undefined, 'info'), true);
  assert.equal(bridge.recordEnabled('warn', 'info'), false);
  helper.setup({ serviceName: 's', environment: 'LOCAL' });
  assert.equal(bridge.recordEnabled(undefined, 'debug'), true);
});

test('B10d emitBridgeRecord on stdout: fatal as ERROR, trace dropped, the record time, the leading error replaces an `exception` field', () => {
  const err = new Error('lead');
  const lines = captureStdout(() => {
    bridge.emitBridgeRecord({ logger: 'b', level: 'trace', minimum: 'debug', message: 'dropped' });
    bridge.emitBridgeRecord({ logger: 'b', level: 'fatal', severityText: 'EMERG', message: 'f', time: new Date(Date.UTC(2026, 10, 2, 1, 2, 3, 4)) });
    bridge.emitBridgeRecord({ logger: 'b', level: 'error', message: 'e', error: err, attributes: { exception: 'theirs', other: new Error('second'), k: 'v' } });
    bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: 42 });
  });
  assert.deepEqual(lines[0], { time: '2026-11-02T01:02:03.004Z', level: 'ERROR', msg: 'f', logger: 'b' });
  assert.deepEqual(Object.keys(lines[1]), ['time', 'level', 'msg', 'logger', 'other', 'k', 'exception']);
  assert.equal(lines[1].exception, err.stack);
  assert.match(lines[1].other, /^Error: second/);
  assert.equal(lines[2].msg, '42');
  assert.equal(lines.length, 3);
});

test('B10d emitBridgeRecord on the OTel path: TRACE/FATAL numbers, source severityText, timestamp, the leading error as the exception', async () => {
  const err = new Error('lead');
  const { stdout, logs } = await otlpRecords(() => {
    bridge.emitBridgeRecord({ logger: 'bridge-scope', level: 'fatal', severityText: 'EMERG', message: 'bridged-fatal', time: new Date(Date.UTC(2026, 10, 2)) });
    bridge.emitBridgeRecord({ logger: 'bridge-scope', level: 'error', message: 'bridged-error', error: err, attributes: { other: new Error('second-err') } });
  });
  assert.deepEqual(stdout, []);
  assert.match(logs, /bridged-fatal/);
  assert.match(logs, /EMERG/);
  assert.match(logs, /bridge-scope/);
  assert.match(logs, /exception\.message[\s\S]*lead/);
  assert.match(logs, /Error: second-err/, 'a second Error is written as its stack text');
});

test('B10d emitBridgeRecord never throws: an unlistable attributes object is reported once, without its values', () => {
  const unlistable = new Proxy({}, { ownKeys() { throw new Error('secret-token'); } });
  const lines = captureStdout(() => bridge.emitBridgeRecord({ logger: 'b', level: 'warn', message: 'm', attributes: unlistable }));
  assert.deepEqual(lines, []);
  assert.ok(records.some((r) => r.line.includes('a helper log record could not be written and was dropped')));
  assert.ok(!records.some((r) => r.line.includes('secret-token')));
});

// ── B10d fix round 2026-11-03 ───────────────────────────────────────────

const { otlpReceiver } = require('../../../test/otlp-logs.cjs');

async function decodedRecords(fn) {
  const receiver = await otlpReceiver();
  try {
    process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
    process.env.OTEL_TRACES_EXPORTER = 'none';
    process.env.OTEL_METRICS_EXPORTER = 'none';
    helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: receiver.endpoint, otlpProtocol: 'http/protobuf' });
    const stdout = captureStdout(fn);
    await helper.shutdown();
    return { stdout, logs: receiver.logs() };
  } finally {
    await receiver.close();
  }
}

test('B10d fix round: a bridged record\'s context decides its trace (captured, or ROOT_CONTEXT = uncorrelated), not the context active at write', async () => {
  let captured;
  let active;
  const { stdout, logs } = await decodedRecords(() => {
    const tracer = api.trace.getTracer('t');
    tracer.startActiveSpan('call', (span) => {
      captured = bridge.activeContext();
      span.end();
    });
    tracer.startActiveSpan('delivery', (span) => {
      active = span.spanContext();
      bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: 'captured', context: captured });
      bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: 'root', context: bridge.ROOT_CONTEXT });
      bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: 'active' });
      span.end();
    });
  });
  assert.deepEqual(stdout, []);
  const byBody = Object.fromEntries(logs.map((r) => [r.body, r]));
  assert.equal(byBody.captured.traceId, api.trace.getSpanContext(captured).traceId);
  assert.notEqual(byBody.captured.traceId, active.traceId);
  assert.equal(byBody.root.traceId, '');
  assert.equal(byBody.active.traceId, active.traceId);
});

test('B10d fix round: a non-string body is an OTel map and stdout JSON text; no message: no body; the leading error drops an `exception` attribute on OTel too', async () => {
  const lines = captureStdout(() => {
    bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: { a: 1, nested: [1, 'x'] } });
    bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: undefined });
    bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: 'text' });
  });
  assert.deepEqual(lines.map((l) => l.msg), ['{"a":1,"nested":[1,"x"]}', '', 'text']);
  const { logs } = await decodedRecords(() => {
    bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: { a: 1, nested: [1, 'x'] } });
    bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: undefined, attributes: { k: 'v' } });
    bridge.emitBridgeRecord({ logger: 'b', level: 'error', message: 'with-error', error: new Error('lead'), attributes: { exception: 'theirs', k: 'v' } });
  });
  assert.deepEqual(logs[0].body, { a: 1, nested: [1, 'x'] });
  assert.equal(logs[1].body, undefined);
  assert.deepEqual(logs[1].attributes, { k: 'v' });
  assert.equal(logs[2].attributes.exception, undefined);
  assert.equal(logs[2].attributes['exception.message'], 'lead');
  assert.equal(logs[2].attributes.k, 'v');
});

test('B10d fix round: flushBridgeRecords() resolves on stdout, flushes the OTLP pipeline, and reports a failure once without throwing', async () => {
  await bridge.flushBridgeRecords();
  const receiver = await otlpReceiver();
  try {
    process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
    process.env.OTEL_TRACES_EXPORTER = 'none';
    process.env.OTEL_METRICS_EXPORTER = 'none';
    helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: receiver.endpoint, otlpProtocol: 'http/protobuf' });
    bridge.emitBridgeRecord({ logger: 'b', level: 'warn', message: 'flushed' });
    await bridge.flushBridgeRecords();
    assert.deepEqual(receiver.logs().map((r) => r.body), ['flushed']);
    const { loggerProvider } = testing.getProviders();
    const original = loggerProvider.forceFlush;
    loggerProvider.forceFlush = () => Promise.reject(new Error('flush-broke'));
    try {
      await bridge.flushBridgeRecords();
      // A rejection that is not an Error: still resolves (reported once already).
      loggerProvider.forceFlush = () => Promise.reject('not-an-error');
      await bridge.flushBridgeRecords();
    } finally {
      loggerProvider.forceFlush = original;
    }
    assert.ok(records.some((r) => r.line.includes('the helper logs pipeline could not be flushed') && JSON.stringify(r).includes('flush-broke')), JSON.stringify(records));
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
});

// ── B10d fix round 2, 2026-11-03 ────────────────────────────────────────

/** A setup exporting logs to a fresh receiver; returns the receiver. */
async function logsSetup() {
  const receiver = await otlpReceiver();
  process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
  process.env.OTEL_TRACES_EXPORTER = 'none';
  process.env.OTEL_METRICS_EXPORTER = 'none';
  helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: receiver.endpoint, otlpProtocol: 'http/protobuf' });
  return receiver;
}

test('B10d fix round 2 HIGH-2: two overlapping bridge flushes, then shutdown(): both records arrive and both flushes resolve', async () => {
  const receiver = await logsSetup();
  try {
    bridge.emitBridgeRecord({ logger: 'b', level: 'warn', message: 'overlap-1' });
    const first = bridge.flushBridgeRecords();
    bridge.emitBridgeRecord({ logger: 'b', level: 'warn', message: 'overlap-2' });
    const second = bridge.flushBridgeRecords();
    assert.notEqual(first, second, 'the second waits for the first, then flushes again');
    await helper.shutdown();
    await Promise.all([first, second]);
  } finally {
    await receiver.close();
  }
  assert.deepEqual(receiver.logs().map((r) => r.body).sort(), ['overlap-1', 'overlap-2']);
});

test('B10d fix round 2 HIGH-2: N un-awaited flushes interleaved with N records, then shutdown(): all N arrive; requests during one wait share it', async () => {
  const receiver = await logsSetup();
  const N = 20;
  const flushes = [];
  try {
    for (let i = 0; i < N; i++) {
      bridge.emitBridgeRecord({ logger: 'b', level: 'warn', message: `record-${i}` });
      flushes.push(bridge.flushBridgeRecords());
    }
    assert.equal(new Set(flushes.slice(1)).size, 1, 'every request after the first shares the one queued flush');
    await helper.shutdown();
    await Promise.all(flushes);
  } finally {
    await receiver.close();
  }
  assert.equal(receiver.logs().length, N);
});

test('B10d fix round 2: a body that converts to a string (bigint, Error) is written as that text; emitRecord converts nothing below the level', async () => {
  const err = new Error('as-body');
  const lines = captureStdout(() => {
    bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: 10n });
    bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: err });
  });
  assert.deepEqual(lines.map((l) => l.msg), ['10', err.stack]);
  let converted = false;
  const hostile = { toString() { converted = true; return 'x'; } };
  const { emitRecord } = require('../dist/internal/logging.js');
  captureStdout(() => emitRecord('b', 'error', 'info', hostile));
  assert.equal(converted, false, 'a dropped record\'s message is never converted');
  const receiver = await logsSetup();
  try {
    bridge.emitBridgeRecord({ logger: 'b', level: 'info', message: 10n });
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  assert.equal(receiver.logs()[0].body, '10');
});

test('B10d fix round 2 HIGH-2: shutdown() during a single in-flight bridge flush waits for it; the record arrives', async () => {
  const receiver = await logsSetup();
  try {
    bridge.emitBridgeRecord({ logger: 'b', level: 'warn', message: 'single-in-flight' });
    const flush = bridge.flushBridgeRecords();
    bridge.emitBridgeRecord({ logger: 'b', level: 'warn', message: 'after-the-flush-started' });
    await helper.shutdown();
    await flush;
  } finally {
    await receiver.close();
  }
  assert.deepEqual(receiver.logs().map((r) => r.body).sort(), ['after-the-flush-started', 'single-in-flight']);
});

// ── B10d final round, 2026-11-03 ────────────────────────────────────────

test('B10d final round: flush serialization is per provider — a hung provider\'s flush does not hold a new setup\'s flush or shutdown', async () => {
  const http = require('node:http');
  const hung = http.createServer(() => { /* never answers */ });
  await new Promise((resolve) => hung.listen(0, '127.0.0.1', resolve));
  const good = await otlpReceiver();
  try {
    process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
    process.env.OTEL_TRACES_EXPORTER = 'none';
    process.env.OTEL_METRICS_EXPORTER = 'none';
    helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: `http://127.0.0.1:${hung.address().port}`, otlpProtocol: 'http/protobuf', exportTimeoutMs: 3000 });
    bridge.emitBridgeRecord({ logger: 'b', level: 'warn', message: 'to-the-hung-receiver' });
    const hungFlush = bridge.flushBridgeRecords();
    await assert.rejects(helper.shutdown(500), /did not finish within 500 ms/);
    helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: good.endpoint, otlpProtocol: 'http/protobuf' });
    bridge.emitBridgeRecord({ logger: 'b', level: 'warn', message: 'to-the-good-receiver' });
    const started = Date.now();
    await bridge.flushBridgeRecords();
    assert.ok(Date.now() - started < 1500, `the new provider's flush did not wait for the hung one (${Date.now() - started} ms)`);
    assert.deepEqual(good.logs().map((r) => r.body), ['to-the-good-receiver']);
    await helper.shutdown();
    await hungFlush;
  } finally {
    hung.closeAllConnections();
    await new Promise((resolve) => hung.close(resolve));
    await good.close();
  }
});

test('B10d final round: a flush failure is reported redacted, and read guarded — the flush never rejects', async () => {
  const receiver = await logsSetup();
  try {
    const { loggerProvider } = testing.getProviders();
    const original = loggerProvider.forceFlush;
    loggerProvider.forceFlush = () => Promise.reject(new Error('export to https://user:hunter2@collector.example.com/v1/logs?token=abc failed'));
    try {
      await bridge.flushBridgeRecords();
    } finally {
      loggerProvider.forceFlush = original;
    }
    const report = JSON.stringify(records.filter((r) => r.line.includes('could not be flushed')));
    assert.match(report, /collector\.example\.com/);
    assert.doesNotMatch(report, /hunter2|token=abc/);
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
});

test('B10d final round: a failure whose message cannot be read is reported by its type', async () => {
  const receiver = await logsSetup();
  try {
    const { loggerProvider } = testing.getProviders();
    const original = loggerProvider.forceFlush;
    const unreadable = new Error('x');
    Object.defineProperty(unreadable, 'message', { get() { throw new Error('secret-token'); } });
    const numeric = new Error('y');
    Object.defineProperty(numeric, 'message', { value: 42 });
    loggerProvider.forceFlush = () => Promise.reject(unreadable);
    try {
      await bridge.flushBridgeRecords();
      loggerProvider.forceFlush = () => Promise.reject(numeric);
      await bridge.flushBridgeRecords();
    } finally {
      loggerProvider.forceFlush = original;
    }
    const report = JSON.stringify(records.filter((r) => r.line.includes('could not be flushed')));
    assert.match(report, /object/);
    assert.doesNotMatch(report, /secret-token/);
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
});
