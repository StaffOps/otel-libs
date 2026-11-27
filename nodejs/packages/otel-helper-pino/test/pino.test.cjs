'use strict';
// createPinoDestination() (task B10d; spec JS6, design D-JS6; contract D6):
// per record the OTel Logs API while the helper's logs exporter is active,
// else one stdout JSON line of the core shape; real pino, the OTLP path
// decoded on a local receiver.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const api = require('@opentelemetry/api');
const pino = require('pino');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { createPinoDestination } = require('@staffops/otel-helper-pino');
const { otlpReceiver, captureStdout } = require('../../../test/otlp-logs.cjs');

const VARS = [
  'OTEL_SERVICE_NAME', 'OTEL_RESOURCE_ATTRIBUTES', 'SERVICE_NAME', 'ENVIRONMENT', 'OTEL_HELPER_DEBUG_LEVEL', 'OTEL_SDK_DISABLED',
  'OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', 'OTEL_EXPORTER_OTLP_PROTOCOL', 'OTEL_EXPORTER_OTLP_COMPRESSION',
  'OTEL_LOGS_EXPORTER', 'OTEL_TRACES_EXPORTER', 'OTEL_METRICS_EXPORTER',
];
process.env.OTEL_HELPER_METRICS_PORT = '0';
const TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const SEVERITY = { TRACE: 1, DEBUG: 5, INFO: 9, WARN: 13, ERROR: 17, FATAL: 21 };

let saved;
let reports;

test.beforeEach(() => {
  saved = Object.fromEntries(VARS.map((k) => [k, process.env[k]]));
  for (const k of VARS) delete process.env[k];
  testing.reset();
  reports = [];
  testing.setDiagnosticSink((line) => reports.push(line));
});

test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

/** A pino logger at `trace` writing only to the helper destination. */
function logger(pinoOptions = {}, destinationOptions) {
  return pino({ level: 'trace', ...pinoOptions }, createPinoDestination(destinationOptions));
}

/** Starts a setup exporting logs over OTLP/HTTP to a fresh receiver. */
async function otlpSetup(extra = {}) {
  const receiver = await otlpReceiver();
  process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
  process.env.OTEL_TRACES_EXPORTER = 'none';
  process.env.OTEL_METRICS_EXPORTER = 'none';
  helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: receiver.endpoint, otlpProtocol: 'http/protobuf', ...extra });
  return receiver;
}

test('JS6 createPinoDestination: options are validated (TypeError naming the option); a pino destination with metadata', () => {
  for (const [options, message] of [
    [null, /^createPinoDestination\(\): options must be an object, got null$/],
    ['x', /options must be an object, got string/],
    [[], /options must be an object, got array/],
    [{ name: '' }, /name must be a non-empty string, got ""/],
    [{ name: 42 }, /name must be a non-empty string, got number/],
    [{ minimumLogLevel: 'trace' }, /minimumLogLevel must be one of debug, info, warn, error, got "trace"/],
    [{ minimumLogLevel: 1 }, /minimumLogLevel must be one of debug, info, warn, error, got number/],
  ]) {
    assert.throws(() => createPinoDestination(options), (err) => err instanceof TypeError && message.test(err.message), JSON.stringify(options));
  }
  const destination = createPinoDestination();
  assert.equal(typeof destination.write, 'function');
  assert.equal(destination[Symbol.for('pino.metadata')], true);
  assert.equal(createPinoDestination(undefined)[Symbol.for('pino.metadata')], true);
});

test('D6 stdout before setup(): one line of the core shape, identical to createLogger for the same record', () => {
  const lines = captureStdout(() => {
    logger({ name: 'orders' }).warn({ 'order.id': 'o-1', cart: { sku: 'x' }, n: 3, ok: true, none: null }, 'paid %s', 'now');
    helper.createLogger({ name: 'orders' }).warn('paid now', { 'order.id': 'o-1', cart: { sku: 'x' }, n: 3, ok: true, none: null });
  });
  assert.equal(lines.length, 2);
  const [fromPino, fromCore] = lines;
  assert.match(fromPino.time, TIME);
  assert.deepEqual(Object.keys(fromPino), Object.keys(fromCore));
  assert.deepEqual({ ...fromPino, time: 'T' }, { ...fromCore, time: 'T' });
  assert.deepEqual(fromPino, { time: fromPino.time, level: 'WARN', msg: 'paid now', logger: 'orders', 'order.id': 'o-1', cart: { sku: 'x' }, n: 3, ok: true, none: null });
});

test('D6 stdout: pino `pid`, `hostname`, `level`, `time` are not fields; child bindings are; default logger name `pino`, option `name`', () => {
  const lines = captureStdout(() => {
    logger().child({ tenant: 't-1' }).info('a');
    logger({}, { name: 'svc' }).info({ k: 'v' });
  });
  assert.deepEqual(Object.keys(lines[0]), ['time', 'level', 'msg', 'logger', 'tenant']);
  assert.equal(lines[0].logger, 'pino');
  assert.deepEqual({ ...lines[1], time: 'T' }, { time: 'T', level: 'INFO', msg: '', logger: 'svc', k: 'v' });
});

test('R4 levels: trace always dropped, debug by the installed level, fatal written as ERROR on stdout; the helper level applies on top of pino\'s', () => {
  const log = logger();
  let lines = captureStdout(() => {
    log.trace('t');
    log.debug('d');
    log.info('i');
    log.warn('w');
    log.error('e');
    log.fatal('f');
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['i', 'INFO'], ['w', 'WARN'], ['e', 'ERROR'], ['f', 'ERROR']]);
  // A fixed minimum: debug passes, trace still never does (below every helper level, as Go and Python).
  lines = captureStdout(() => {
    const fixed = logger({}, { minimumLogLevel: 'debug' });
    fixed.trace('t');
    fixed.debug('d');
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['d', 'DEBUG']]);
  // pino's own level still filters first.
  lines = captureStdout(() => pino({ level: 'error' }, createPinoDestination()).warn('w'));
  assert.deepEqual(lines, []);
  // Custom levels fall in the bucket below them: 35 is info, 45 warn.
  lines = captureStdout(() => {
    const custom = logger({ customLevels: { audit: 35, notice: 45 } });
    custom.audit('a');
    custom.notice('n');
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['a', 'INFO'], ['n', 'WARN']]);
});

test('R4 the installed level is read per record (setup ENVIRONMENT=LOCAL: debug; PRD: warn), also with no exporter', () => {
  const log = logger();
  helper.setup({ serviceName: 's', environment: 'LOCAL' });
  let lines = captureStdout(() => log.debug('local-debug'));
  assert.deepEqual(lines.map((l) => l.msg), ['local-debug']);
  return helper.shutdown().then(() => {
    testing.reset();
    helper.setup({ serviceName: 's', environment: 'PRD' });
    lines = captureStdout(() => {
      log.info('prd-info');
      log.warn('prd-warn');
    });
    assert.deepEqual(lines.map((l) => l.msg), ['prd-warn']);
  });
});

test('errors: pino\'s serialized `err` becomes `exception` (the stack), under a custom errorKey too; a non-error `err` stays a field', () => {
  const err = new Error('kaput');
  const lines = captureStdout(() => {
    logger().error(err);
    logger().error({ err, order: 'o-2' }, 'failed');
    logger({ errorKey: 'error' }).error({ error: err }, 'custom-key');
    logger().warn({ err: 'just text' }, 'not-an-error');
  });
  assert.equal(lines[0].msg, 'kaput');
  assert.equal(lines[0].exception, err.stack);
  assert.equal(lines[0].err, undefined);
  assert.deepEqual(Object.keys(lines[1]), ['time', 'level', 'msg', 'logger', 'order', 'exception']);
  assert.equal(lines[1].exception, err.stack);
  assert.equal(lines[2].exception, err.stack);
  assert.equal(lines[2].error, undefined);
  assert.equal(lines[3].err, 'just text');
  assert.equal(lines[3].exception, undefined);
});

test('errors: an error-like `err` without a stack is written `type: message`; an `exception` field is replaced by the record error', () => {
  const lines = captureStdout(() => {
    const destination = createPinoDestination();
    destination.lastLevel = 50;
    destination.write(`${JSON.stringify({ level: 50, msg: 'm', err: { type: 'TypeError', message: 'bad' }, exception: 'mine' })}\n`);
    destination.write(`${JSON.stringify({ level: 50, msg: 'm', err: { stack: 'only-a-stack', code: 'E1' } })}\n`);
    destination.write(`${JSON.stringify({ level: 50, msg: 'm', err: { type: 'T', message: 7 } })}\n`);
  });
  assert.equal(lines[0].exception, 'TypeError: bad');
  assert.equal(lines[1].exception, 'only-a-stack');
  assert.deepEqual(lines[2].err, { type: 'T', message: 7 });
});

test('pino options the destination honors: messageKey, name field, redact, serializers, formatters.log', () => {
  const lines = captureStdout(() => {
    logger({ messageKey: 'message' }).info({ k: 1 }, 'custom-message-key');
    logger({ name: 'billing', redact: ['card.number', 'password'] }).info({ card: { number: '4111', brand: 'v' }, password: 'p' }, 'redacted');
    logger({ serializers: { user: (u) => ({ id: u.id }) } }).info({ user: { id: 'u1', email: 'a@example.com' } }, 'serialized');
    logger({ formatters: { log: (o) => ({ ...o, extra: 'added' }) } }).info('formatted');
  });
  assert.deepEqual({ ...lines[0], time: 'T' }, { time: 'T', level: 'INFO', msg: 'custom-message-key', logger: 'pino', k: 1 });
  assert.equal(lines[1].logger, 'billing');
  assert.deepEqual(lines[1].card, { number: '[Redacted]', brand: 'v' });
  assert.equal(lines[1].password, '[Redacted]');
  assert.deepEqual(lines[2].user, { id: 'u1' });
  assert.equal(lines[3].extra, 'added');
});

test('pino.multistream: the destination keeps its metadata and the other stream still gets the line (the helper never rewrites it)', () => {
  const other = [];
  const lines = captureStdout(() => {
    const log = pino({ level: 'trace' }, pino.multistream([{ stream: createPinoDestination(), level: 'trace' }, { stream: { write: (l) => other.push(l) }, level: 'trace' }]));
    log.fatal({ k: 'v' }, 'both');
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level, l.k]), [['both', 'ERROR', 'v']]);
  assert.equal(other.length, 1);
  assert.equal(JSON.parse(other[0]).msg, 'both');
});

test('D6 OTLP active: Logs API only, decoded on the receiver (severity, text, body, maps, exception, scope, trace correlation)', async () => {
  const receiver = await otlpSetup();
  let ids;
  const err = new Error('kaput');
  try {
    const stdout = captureStdout(() => {
      const log = logger({ name: 'orders' });
      log.debug('below-the-level');
      api.trace.getTracer('t').startActiveSpan('sp', (span) => {
        ids = span.spanContext();
        log.warn({ 'order.id': 'o-1', cart: { lineItem: 'sku-9' }, rows: [{ sku: 'x' }, 2], n: 1.5 }, 'otlp-record');
        log.error({ err }, 'failed');
        log.fatal('fatal-record');
        span.end();
      });
      logger({}, { name: 'fallback-name' }).info('no-span');
    });
    await helper.shutdown();
    assert.deepEqual(stdout, []);
  } finally {
    await receiver.close();
  }
  const records = receiver.logs();
  assert.deepEqual(records.map((r) => r.body), ['otlp-record', 'failed', 'fatal-record', 'no-span']);
  const [warn, error, fatal, plain] = records;
  assert.equal(warn.scope, 'orders');
  assert.equal(warn.severityNumber, SEVERITY.WARN);
  assert.equal(warn.severityText, 'WARN');
  assert.equal(warn.traceId, ids.traceId);
  assert.equal(warn.spanId, ids.spanId);
  // Maps and arrays arrive as OTLP kvlist/array values, not JSON text.
  assert.deepEqual(warn.attributes, { 'order.id': 'o-1', cart: { lineItem: 'sku-9' }, rows: [{ sku: 'x' }, 2], n: 1.5 });
  assert.equal(error.severityNumber, SEVERITY.ERROR);
  assert.equal(error.attributes['exception.type'], 'Error');
  assert.equal(error.attributes['exception.message'], 'kaput');
  assert.equal(error.attributes['exception.stacktrace'], err.stack);
  assert.equal(error.attributes.err, undefined);
  assert.equal(fatal.severityNumber, SEVERITY.FATAL);
  assert.equal(fatal.severityText, 'FATAL');
  assert.equal(plain.scope, 'fallback-name');
  assert.equal(plain.traceId, '');
  assert.ok(plain.timeUnixNano > 0n, 'record timestamp set');
  assert.equal(warn.resource['service.name'], 's');
});

test('severityText is the logger\'s own label (custom levels), the number its bucket', async () => {
  const receiver = await otlpSetup();
  try {
    logger({ customLevels: { audit: 35 } }).audit('custom');
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  const [record] = receiver.logs();
  assert.equal(record.severityText, 'AUDIT');
  assert.equal(record.severityNumber, SEVERITY.INFO);
});

test('D6 the destination switches per record across shutdown() and re-setup(): stdout, OTLP, stdout, OTLP', async () => {
  const log = logger();
  const first = await otlpReceiver();
  const second = await otlpReceiver();
  const stdout = [];
  try {
    stdout.push(...captureStdout(() => log.info('before-setup')));
    process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
    process.env.OTEL_TRACES_EXPORTER = 'none';
    process.env.OTEL_METRICS_EXPORTER = 'none';
    helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: first.endpoint, otlpProtocol: 'http/protobuf' });
    stdout.push(...captureStdout(() => log.info('first-setup')));
    await helper.shutdown();
    stdout.push(...captureStdout(() => log.info('after-shutdown')));
    helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: second.endpoint, otlpProtocol: 'http/protobuf' });
    stdout.push(...captureStdout(() => log.info('second-setup')));
    await helper.shutdown();
  } finally {
    await first.close();
    await second.close();
  }
  assert.deepEqual(stdout.map((l) => l.msg), ['before-setup', 'after-shutdown']);
  assert.deepEqual(first.logs().map((r) => r.body), ['first-setup']);
  assert.deepEqual(second.logs().map((r) => r.body), ['second-setup']);
});

test('OTEL_LOGS_EXPORTER=none: stdout even with an endpoint', async () => {
  process.env.OTEL_LOGS_EXPORTER = 'none';
  const receiver = await otlpSetup();
  try {
    const lines = captureStdout(() => logger().info('to-stdout'));
    assert.deepEqual(lines.map((l) => l.msg), ['to-stdout']);
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  assert.deepEqual(receiver.logs(), []);
});

test('hostile input: write() never throws — not JSON, not an object, no metadata, a hostile lastLogger, __proto__, deep nesting', () => {
  const destination = createPinoDestination();
  const hostile = new Proxy({}, { get() { throw new Error('secret-token'); } });
  let deep = 'leaf';
  for (let i = 0; i < 150; i++) deep = { d: deep };
  const lines = captureStdout(() => {
    assert.equal(destination.write('not json at all\n'), true);
    assert.equal(destination.write('[1,2]'), true);
    assert.equal(destination.write(Buffer.from('{"level":40,"msg":"from-a-buffer"}')), true);
    destination.lastLevel = 'x';
    destination.lastLogger = hostile;
    destination.write('{"level":50,"msg":"hostile-logger"}');
    destination.lastLevel = undefined;
    destination.lastLogger = undefined;
    destination.write('{"msg":"no-level"}');
    destination.write('{"level":30,"msg":"proto","__proto__":{"polluted":true}}');
    destination.write(JSON.stringify({ level: 30, msg: 'deep', deep }));
    destination.write(JSON.stringify({ level: 30, msg: { not: 'a string' } }));
    destination.write({ toString() { throw new Error('secret-token'); } });
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [
    ['not json at all', 'INFO'],
    ['[1,2]', 'INFO'],
    ['from-a-buffer', 'WARN'],
    ['hostile-logger', 'ERROR'],
    ['no-level', 'INFO'],
    ['proto', 'INFO'],
    ['deep', 'INFO'],
    ['{"not":"a string"}', 'INFO'],
    ['[unreadable line]', 'INFO'],
  ]);
  assert.equal(lines[5].polluted, undefined);
  assert.deepEqual(Object.getOwnPropertyDescriptor(lines[5], '__proto__').value, { polluted: true });
  assert.equal({}.polluted, undefined);
  assert.match(JSON.stringify(lines[6]), /<max-depth-exceeded>/);
  assert.ok(!reports.some((r) => r.includes('secret-token')));
});

test('D14: a missing pino is named; another load failure is reported verbatim (child process)', () => {
  const run = (failure) => spawnSync(process.execPath, ['-e', `
    const Module = require('node:module');
    const resolve = Module._resolveFilename;
    Module._resolveFilename = function (request, ...rest) {
      if (request === 'pino') {
        ${failure}
      }
      return resolve.call(this, request, ...rest);
    };
    const { createPinoDestination } = require('@staffops/otel-helper-pino');
    try { createPinoDestination(); } catch (err) { console.log(JSON.stringify({ name: err.name, message: err.message, cause: Boolean(err.cause) })); }
    try { createPinoDestination(); } catch (err) { console.log(JSON.stringify({ again: err.message })); }
  `], { cwd: path.join(__dirname, '..'), encoding: 'utf8' });
  const missing = run(`const e = new Error("Cannot find module 'pino'"); e.code = 'MODULE_NOT_FOUND'; throw e;`);
  const [first, again] = missing.stdout.trim().split('\n').map((l) => JSON.parse(l));
  assert.deepEqual(first, { name: 'Error', message: "@staffops/otel-helper-pino is installed but the library 'pino' is not. Install it (e.g. npm install pino)", cause: true });
  assert.equal(again.again, first.message);
  const other = run(`throw new SyntaxError('Unexpected token in pino');`);
  assert.deepEqual(JSON.parse(other.stdout.trim().split('\n')[0]), { name: 'Error', message: '@staffops/otel-helper-pino: failed to load pino: Unexpected token in pino', cause: true });
  const nested = run(`const e = new Error("Cannot find module 'sonic-boom'"); e.code = 'MODULE_NOT_FOUND'; throw e;`);
  assert.match(JSON.parse(nested.stdout.trim().split('\n')[0]).message, /failed to load pino: Cannot find module 'sonic-boom'/);
  const thrown = run(`throw 'a string';`);
  assert.match(JSON.parse(thrown.stdout.trim().split('\n')[0]).message, /failed to load pino: a string/);
});

// ── fix round 2026-11-03 ─────────────────────────────────────────────────

test('fix round: pino(createPinoDestination()) with one argument writes through the destination (writable: true)', () => {
  const destination = createPinoDestination();
  assert.equal(destination.writable, true);
  const lines = captureStdout(() => pino(destination).info({ k: 'v' }, 'one-argument'));
  assert.deepEqual(lines.map((l) => [l.msg, l.logger, l.k]), [['one-argument', 'pino', 'v']]);
});

test('fix round: D14 another load failure keeps only its first line (the require stack lists local paths), cause kept', () => {
  const res = spawnSync(process.execPath, ['-e', `
    const Module = require('node:module');
    const resolve = Module._resolveFilename;
    Module._resolveFilename = function (request, ...rest) {
      if (request === 'pino') { const e = new Error("Cannot find module 'sonic-boom'\\nRequire stack:\\n- /home/someone/app/x.js"); e.code = 'MODULE_NOT_FOUND'; throw e; }
      return resolve.call(this, request, ...rest);
    };
    try { require('@staffops/otel-helper-pino').createPinoDestination(); } catch (err) { console.log(JSON.stringify({ message: err.message, cause: err.cause.message })); }
  `], { cwd: path.join(__dirname, '..'), encoding: 'utf8' });
  const out = JSON.parse(res.stdout.trim());
  assert.equal(out.message, "@staffops/otel-helper-pino: failed to load pino: Cannot find module 'sonic-boom'");
  assert.match(out.cause, /Require stack/);
});

test('fix round: a non-string message is a map body on OTel and its JSON text on stdout; no message: no OTel body, msg ""', async () => {
  let lines = captureStdout(() => {
    logger().info({ msg: { order: 'o-1', n: 2 } });
    logger().info({ k: 1 });
  });
  assert.deepEqual(lines.map((l) => l.msg), ['{"order":"o-1","n":2}', '']);
  const receiver = await otlpSetup();
  try {
    lines = captureStdout(() => {
      logger().info({ msg: { order: 'o-1', n: 2 } });
      logger().info({ k: 1 });
    });
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  assert.deepEqual(lines, []);
  const [map, none] = receiver.logs();
  assert.deepEqual(map.body, { order: 'o-1', n: 2 });
  assert.equal(none.body, undefined);
  assert.deepEqual(none.attributes, { k: 1 });
});

test('fix round: pino err extras (statusCode, details, code) stay a map under the error key; name preferred over type', async () => {
  class HttpError extends Error {
    constructor(message) {
      super(message);
      this.name = 'HttpError';
      this.statusCode = 503;
      this.details = { upstream: 'db' };
      this.code = 'EUPSTREAM';
    }
  }
  const err = new HttpError('upstream down');
  const lines = captureStdout(() => {
    logger().error({ err }, 'failed');
    logger({ errorKey: 'error' }).error({ error: err }, 'custom-key');
    logger().error(new Error('plain'));
  });
  assert.deepEqual(lines[0].err, { statusCode: 503, details: { upstream: 'db' }, code: 'EUPSTREAM' });
  assert.equal(lines[0].exception, err.stack);
  assert.deepEqual(lines[1].error, { statusCode: 503, details: { upstream: 'db' }, code: 'EUPSTREAM' });
  assert.equal(lines[2].err, undefined, 'no extras: no err field');
  const receiver = await otlpSetup();
  try {
    logger().error({ err }, 'failed');
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  const [record] = receiver.logs();
  assert.equal(record.attributes['exception.message'], 'upstream down');
  // A string `code` is the SDK's exception.type (core rule, D-JS14); without it the name wins over pino's `type`.
  assert.equal(record.attributes['exception.type'], 'EUPSTREAM');
  assert.deepEqual(record.attributes.err, { statusCode: 503, details: { upstream: 'db' }, code: 'EUPSTREAM' });
  const named = captureStdout(() => {
    const destination = createPinoDestination();
    destination.write(JSON.stringify({ level: 50, msg: 'm', err: { type: 'Error', name: 'Custom', message: 'x' } }));
  });
  assert.equal(named[0].exception, 'Custom: x');
});

test('fix round: the logger name comes from the logger\'s name binding; a different per-call `name` stays a field', () => {
  const lines = captureStdout(() => {
    const log = logger({ name: 'orders' });
    log.info({ name: 'other' }, 'per-call');
    log.child({ tenant: 't' }).info('child');
    logger().info({ name: 'call-only' }, 'unnamed-logger');
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.logger, l.name]), [['per-call', 'orders', 'other'], ['child', 'orders', undefined], ['unnamed-logger', 'pino', 'call-only']]);
});

test('fix round: messageKey/errorKey of a logger made by another copy of pino (other Symbol instances)', () => {
  const pinoFiles = Object.keys(require.cache).filter((k) => /[\\/]node_modules[\\/]pino[\\/]/.test(k));
  const saved = Object.fromEntries(pinoFiles.map((k) => [k, require.cache[k]]));
  for (const k of pinoFiles) delete require.cache[k];
  let otherPino;
  try {
    otherPino = require('pino');
  } finally {
    for (const k of Object.keys(require.cache).filter((key) => /[\\/]node_modules[\\/]pino[\\/]/.test(key))) delete require.cache[k];
    Object.assign(require.cache, saved);
  }
  assert.notEqual(otherPino, pino);
  assert.notEqual(otherPino.symbols.messageKeySym, pino.symbols.messageKeySym);
  const err = new Error('other-copy');
  const lines = captureStdout(() => {
    const log = otherPino({ messageKey: 'message', errorKey: 'problem' }, createPinoDestination());
    log.error({ problem: err }, 'from-another-pino');
  });
  assert.equal(lines[0].msg, 'from-another-pino');
  assert.equal(lines[0].exception, err.stack);
  assert.equal(lines[0].message, undefined);
});

test('fix round: pid/hostname dropped only when they are this process\'s; big integers keep their digits; a multi-line chunk is one record per line', () => {
  const destination = createPinoDestination();
  const lines = captureStdout(() => {
    destination.write(JSON.stringify({ level: 30, msg: 'foreign-base', pid: 1, hostname: 'elsewhere.example.com' }));
    destination.write(`${JSON.stringify({ level: 30, msg: 'own-base', pid: process.pid, hostname: require('node:os').hostname() })}\n`);
    destination.write('{"level":30,"msg":"big","id":12345678901234567890,"neg":-98765432109876543210,"safe":42,"f":1.5e300}\n');
    destination.write('{"level":30,"msg":"first"}\n{"level":50,"msg":"second"}\n\n');
    destination.lastLevel = 40;
    destination.write('{"msg":"no-own-level"}\n{"level":30,"msg":"own-level"}');
    // Hostile or odd loggers in the metadata: defaults, never a throw (one line: the metadata level, 40).
    destination.lastLogger = new Proxy({}, { ownKeys() { throw new Error('secret-token'); } });
    destination.write('{"level":30,"msg":"unlistable-logger"}');
    destination.lastLogger = { bindings: () => null };
    destination.write('{"level":30,"msg":"null-bindings","name":"kept"}');
  });
  assert.deepEqual(lines.slice(5).map((l) => [l.msg, l.level, l.logger, l.name]), [
    ['no-own-level', 'WARN', 'pino', undefined], ['own-level', 'INFO', 'pino', undefined], ['unlistable-logger', 'WARN', 'pino', undefined], ['null-bindings', 'WARN', 'pino', 'kept'],
  ]);
  assert.deepEqual(lines.slice(0, 5).map((l) => l.msg), ['foreign-base', 'own-base', 'big', 'first', 'second']);
  assert.equal(lines[0].pid, 1);
  assert.equal(lines[0].hostname, 'elsewhere.example.com');
  assert.equal(lines[1].pid, undefined);
  assert.equal(lines[1].hostname, undefined);
  assert.equal(lines[2].id, '12345678901234567890');
  assert.equal(lines[2].neg, '-98765432109876543210');
  assert.equal(lines[2].safe, 42);
  assert.equal(lines[2].f, 1.5e300);
  assert.deepEqual([lines[3].level, lines[4].level], ['INFO', 'ERROR']);
});

test('fix round: logger.flush(cb) flushes the helper logs pipeline before cb (the record is on the receiver, before shutdown)', async () => {
  const receiver = await otlpSetup();
  try {
    const log = logger();
    log.warn('flushed-record');
    await new Promise((resolve) => log.flush(resolve));
    assert.deepEqual(receiver.logs().map((r) => r.body), ['flushed-record']);
    // Without a callback, and on the stdout path, flush is harmless.
    createPinoDestination().flush();
    await helper.shutdown();
    await new Promise((resolve) => log.flush(resolve));
  } finally {
    await receiver.close();
  }
});

test('fix round: the default export (pino.transport target) throws a clear "not supported" (CJS and ESM)', async () => {
  const message = /pino\.transport\(\{ target: '@staffops\/otel-helper-pino' \}\) is not supported .*pass createPinoDestination\(\) to pino\(\) instead/;
  assert.throws(() => require('@staffops/otel-helper-pino').default(), { message });
  const esm = await import('@staffops/otel-helper-pino');
  assert.throws(() => esm.default(), { message });
});

// ── fix round 2, 2026-11-03 ─────────────────────────────────────────────

test('fix round 2: the bound logger name is cached per logger and follows setBindings()', () => {
  const lines = captureStdout(() => {
    const log = logger({ name: 'first-name' });
    log.info('a');
    log.info('b');
    log.setBindings({ name: 'second-name' });
    log.info('c');
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.logger]), [['a', 'first-name'], ['b', 'first-name'], ['c', 'second-name']]);
});

test('fix round 2: a dropped record is not parsed; short integers skip the big-integer reviver; one trailing newline is one record', () => {
  const destination = createPinoDestination();
  const lines = captureStdout(() => {
    destination.lastLevel = 10; // trace: dropped before parsing
    destination.write('{ not even json');
    destination.lastLevel = 30;
    destination.write('{"level":30,"msg":"short","n":123456789012345}\n');
    destination.write('{"level":30,"msg":"long","n":1234567890123456789}\n');
    destination.write('\n');
    destination.write('plain text\nsecond plain line\n');
    destination.lastLevel = undefined; // no metadata: the line's own level, checked after parsing
    destination.write('{"level":10,"msg":"trace-without-metadata"}');
  });
  assert.deepEqual(lines.map((l) => l.msg), ['short', 'long', 'plain text', 'second plain line']);
  assert.equal(lines[0].n, 123456789012345);
  assert.equal(lines[1].n, '1234567890123456789');
});

test('flush(cb): the callback runs on its own tick, so a throwing callback is an uncaught exception, not an unhandled rejection (child process)', () => {
  const { spawnSync } = require('node:child_process');
  const res = spawnSync(process.execPath, ['-e', `
    process.on('uncaughtException', (e) => { console.log(JSON.stringify({ uncaught: e.message })); });
    process.on('unhandledRejection', (e) => { console.log(JSON.stringify({ rejection: String(e) })); });
    require('@staffops/otel-helper-pino').createPinoDestination().flush(() => { throw new Error('cb boom'); });
  `], { cwd: require('node:path').join(__dirname, '..'), encoding: 'utf8' });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(res.stdout.trim().split('\n').map((l) => JSON.parse(l)), [{ uncaught: 'cb boom' }]);
});
