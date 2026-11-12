'use strict';
// OtelHelperTransport (task B10d; spec JS6, design D-JS6; contract D6): per
// record the OTel Logs API while the helper's logs exporter is active, else
// one stdout JSON line of the core shape; real winston, the OTLP path decoded
// on a local receiver.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const api = require('@opentelemetry/api');
const winston = require('winston');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { OtelHelperTransport } = require('@staffops/otel-helper-winston');
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

/** A winston logger (npm levels at `silly` unless given) writing only to the helper transport. */
function logger(winstonOptions = {}, transportOptions) {
  return winston.createLogger({ level: 'silly', ...winstonOptions, transports: [new OtelHelperTransport(transportOptions)] });
}

async function otlpSetup() {
  const receiver = await otlpReceiver();
  process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
  process.env.OTEL_TRACES_EXPORTER = 'none';
  process.env.OTEL_METRICS_EXPORTER = 'none';
  helper.setup({ serviceName: 's', environment: 'DEV', otelEndpoint: receiver.endpoint, otlpProtocol: 'http/protobuf' });
  return receiver;
}

test('JS6 OtelHelperTransport: helper options are validated (TypeError naming the option); a winston Transport', () => {
  for (const [options, message] of [
    [null, /^OtelHelperTransport: options must be an object, got null$/],
    ['x', /options must be an object, got string/],
    [[], /options must be an object, got array/],
    [{ name: ' ' }, /name must be a non-empty string, got " "/],
    [{ name: 1 }, /name must be a non-empty string, got number/],
    [{ minimumLogLevel: 'silly' }, /minimumLogLevel must be one of debug, info, warn, error, got "silly"/],
    [{ minimumLogLevel: false }, /minimumLogLevel must be one of debug, info, warn, error, got boolean/],
  ]) {
    assert.throws(() => new OtelHelperTransport(options), (err) => err instanceof TypeError && message.test(err.message), JSON.stringify(options));
  }
  const transport = new OtelHelperTransport();
  assert.ok(transport instanceof winston.Transport);
  assert.ok(transport instanceof OtelHelperTransport);
});

test('D6 stdout before setup(): one line of the core shape, identical to createLogger for the same record', () => {
  const lines = captureStdout(() => {
    logger({}, { name: 'orders' }).warn('paid now', { 'order.id': 'o-1', cart: { sku: 'x' }, n: 3, ok: true, none: null });
    helper.createLogger({ name: 'orders' }).warn('paid now', { 'order.id': 'o-1', cart: { sku: 'x' }, n: 3, ok: true, none: null });
  });
  assert.equal(lines.length, 2);
  const [fromWinston, fromCore] = lines;
  assert.match(fromWinston.time, TIME);
  assert.deepEqual(Object.keys(fromWinston), Object.keys(fromCore));
  assert.deepEqual({ ...fromWinston, time: 'T' }, { ...fromCore, time: 'T' });
  assert.deepEqual(fromWinston, { time: fromWinston.time, level: 'WARN', msg: 'paid now', logger: 'orders', 'order.id': 'o-1', cart: { sku: 'x' }, n: 3, ok: true, none: null });
});

test('D6 stdout: default logger name `winston`, defaultMeta and formats become fields, a colorized level still maps', () => {
  const lines = captureStdout(() => {
    logger({ defaultMeta: { service: 'svc' }, format: winston.format.combine(winston.format.timestamp(), winston.format.colorize()) }).error('colored');
  });
  assert.equal(lines.length, 1);
  assert.equal(lines[0].logger, 'winston');
  assert.equal(lines[0].level, 'ERROR');
  assert.equal(lines[0].service, 'svc');
  assert.equal(typeof lines[0].timestamp, 'string');
  assert.deepEqual(Object.keys(lines[0]).slice(0, 4), ['time', 'level', 'msg', 'logger']);
});

test('R4 npm levels: http/verbose are debug, silly is trace (always dropped); the helper level applies on top of winston\'s', () => {
  let lines = captureStdout(() => {
    const log = logger();
    for (const level of ['error', 'warn', 'info', 'http', 'verbose', 'debug', 'silly']) log.log(level, level);
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['error', 'ERROR'], ['warn', 'WARN'], ['info', 'INFO']]);
  lines = captureStdout(() => {
    const log = logger({}, { minimumLogLevel: 'debug' });
    for (const level of ['http', 'verbose', 'debug', 'silly']) log.log(level, level);
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['http', 'DEBUG'], ['verbose', 'DEBUG'], ['debug', 'DEBUG']]);
  // winston's own (transport) level filters first.
  lines = captureStdout(() => winston.createLogger({ transports: [new OtelHelperTransport({ level: 'error' })] }).warn('w'));
  assert.deepEqual(lines, []);
});

test('R4 syslog and cli levels: placed against the error/warn/info/debug anchors (emerg/alert/crit fatal, notice info, help/data info)', () => {
  let lines = captureStdout(() => {
    const log = logger({ levels: winston.config.syslog.levels, level: 'debug' }, { minimumLogLevel: 'debug' });
    for (const level of ['emerg', 'alert', 'crit', 'error', 'warning', 'notice', 'info', 'debug']) log.log(level, level);
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [
    ['emerg', 'ERROR'], ['alert', 'ERROR'], ['crit', 'ERROR'], ['error', 'ERROR'], ['warning', 'WARN'], ['notice', 'INFO'], ['info', 'INFO'], ['debug', 'DEBUG'],
  ]);
  lines = captureStdout(() => {
    const log = logger({ levels: winston.config.cli.levels, level: 'silly' }, { minimumLogLevel: 'debug' });
    for (const level of ['help', 'data', 'info', 'debug', 'prompt', 'verbose', 'input', 'silly']) log.log(level, level);
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['help', 'INFO'], ['data', 'INFO'], ['info', 'INFO'], ['debug', 'DEBUG']]);
  // Partial anchors: above `warn` is warn; below the last anchor without a `debug` one falls back to the name.
  lines = captureStdout(() => {
    const log = logger({ levels: { high: 0, warn: 1, low: 2 }, level: 'low' });
    log.log('high', 'high');
    log.log('warn', 'warn');
    log.log('low', 'low');
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['high', 'WARN'], ['warn', 'WARN'], ['low', 'INFO']]);
  // No anchors at all: by name, unknown names are info.
  lines = captureStdout(() => {
    const log = logger({ levels: { high: 0, low: 1 }, level: 'low' });
    log.log('high', 'high');
    log.log('low', 'low');
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['high', 'INFO'], ['low', 'INFO']]);
});

test('R4 a transport used outside a logger (no levels): the level by name', () => {
  const transport = new OtelHelperTransport({ minimumLogLevel: 'debug' });
  const lines = captureStdout(() => {
    for (const level of ['error', 'warning', 'warn', 'debug', 'info', 'custom']) transport.log({ level, message: level }, () => undefined);
  });
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['error', 'ERROR'], ['warning', 'WARN'], ['warn', 'WARN'], ['debug', 'DEBUG'], ['info', 'INFO'], ['custom', 'INFO']]);
});

test('R4 the installed level is read per record (setup ENVIRONMENT=PRD: warn)', () => {
  const log = logger();
  helper.setup({ serviceName: 's', environment: 'PRD' });
  const lines = captureStdout(() => {
    log.info('prd-info');
    log.warn('prd-warn');
  });
  assert.deepEqual(lines.map((l) => l.msg), ['prd-warn']);
});

test('errors: logger.error(err), logger.error(msg, err) and an Error field become `exception` (the stack)', () => {
  const err = new Error('boom');
  err.code = 'E1';
  const lines = captureStdout(() => {
    const log = logger();
    log.error(err);
    log.error('wrapped', err);
    log.warn('meta', { order: 'o-3', e: err });
  });
  assert.deepEqual({ ...lines[0], time: 'T' }, { time: 'T', level: 'ERROR', msg: 'boom', logger: 'winston', code: 'E1', exception: err.stack });
  assert.deepEqual({ ...lines[1], time: 'T' }, { time: 'T', level: 'ERROR', msg: 'wrapped boom', logger: 'winston', code: 'E1', exception: err.stack });
  assert.deepEqual({ ...lines[2], time: 'T' }, { time: 'T', level: 'WARN', msg: 'meta', logger: 'winston', order: 'o-3', exception: err.stack });
});

test('D6 OTLP active: Logs API only, decoded on the receiver (severity, source text, body, maps, exception, scope, trace correlation)', async () => {
  const receiver = await otlpSetup();
  let ids;
  const err = new Error('kaput');
  try {
    const stdout = captureStdout(() => {
      const log = logger({ defaultMeta: { service: 'svc' } }, { name: 'orders' });
      log.debug('below-the-level');
      api.trace.getTracer('t').startActiveSpan('sp', (span) => {
        ids = span.spanContext();
        log.warn('otlp-record', { 'order.id': 'o-1', cart: { lineItem: 'sku-9' }, rows: [{ sku: 'x' }, 2] });
        log.error('failed', err);
        span.end();
      });
      const syslog = logger({ levels: winston.config.syslog.levels, level: 'debug' }, { name: 'sys' });
      syslog.emerg('fatal-record');
      syslog.notice('notice-record');
    });
    await helper.shutdown();
    assert.deepEqual(stdout, []);
  } finally {
    await receiver.close();
  }
  const records = receiver.logs();
  assert.deepEqual(records.map((r) => r.body), ['otlp-record', 'failed kaput', 'fatal-record', 'notice-record']);
  const [warn, error, emerg, notice] = records;
  assert.equal(warn.scope, 'orders');
  assert.equal(warn.severityNumber, SEVERITY.WARN);
  assert.equal(warn.severityText, 'WARN');
  assert.equal(warn.traceId, ids.traceId);
  assert.equal(warn.spanId, ids.spanId);
  assert.deepEqual(warn.attributes, { 'order.id': 'o-1', cart: { lineItem: 'sku-9' }, rows: [{ sku: 'x' }, 2], service: 'svc' });
  assert.equal(error.severityNumber, SEVERITY.ERROR);
  assert.equal(error.attributes['exception.type'], 'Error');
  assert.equal(error.attributes['exception.message'], 'kaput');
  assert.equal(error.attributes['exception.stacktrace'], err.stack);
  assert.equal(error.attributes.stack, undefined, 'the copied stack is not a field');
  assert.equal(emerg.scope, 'sys');
  assert.equal(emerg.severityNumber, SEVERITY.FATAL);
  assert.equal(emerg.severityText, 'EMERG');
  assert.equal(notice.severityNumber, SEVERITY.INFO);
  assert.equal(notice.severityText, 'NOTICE');
  assert.equal(emerg.traceId, '');
});

test('D6 the transport switches per record across shutdown() and re-setup(): stdout, OTLP, stdout, OTLP', async () => {
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

test('hostile input: log() never throws and always calls next — non-objects, throwing getters, an unlistable Proxy, __proto__', () => {
  const transport = new OtelHelperTransport();
  let nexts = 0;
  const next = () => nexts++;
  const LEVEL = Symbol.for('level');
  const getter = { [LEVEL]: 'info', level: 'info', message: 'getter' };
  Object.defineProperty(getter, 'token', { enumerable: true, get() { throw new Error('secret-token'); } });
  const proto = JSON.parse('{"level":"info","message":"proto","__proto__":{"polluted":true}}');
  const unlistable = new Proxy({}, { ownKeys() { throw new Error('secret-token'); } });
  const hostileMessage = { level: 'warn' };
  Object.defineProperty(hostileMessage, 'message', { get() { throw new Error('secret-token'); } });
  const noPrototype = new Proxy({ level: 'info', message: 'no-prototype' }, { getPrototypeOf() { throw new Error('secret-token'); } });
  const lines = captureStdout(() => {
    transport.log(null, next);
    transport.log('a string', next);
    transport.log(getter, next);
    transport.log(proto, next);
    transport.log(unlistable, next);
    transport.log(hostileMessage, next);
    transport.log({ message: 'no-level' }, next);
    transport.log({ level: 'nonsense', message: 'unknown-level' }, next);
    transport.log(noPrototype, next);
  });
  assert.equal(nexts, 9);
  assert.deepEqual(lines.map((l) => [l.msg, l.level]), [['getter', 'INFO'], ['proto', 'INFO'], ['', 'WARN'], ['no-level', 'INFO'], ['unknown-level', 'INFO'], ['no-prototype', 'INFO']]);
  assert.equal(lines[0].token, '[unreadable accessor]');
  assert.deepEqual(Object.getOwnPropertyDescriptor(lines[1], '__proto__').value, { polluted: true });
  assert.equal({}.polluted, undefined);
  assert.ok(reports.some((r) => r.includes('a helper log record could not be written and was dropped')), reports.join('\n'));
  assert.ok(!reports.some((r) => r.includes('secret-token')));
});

test('D14: a missing winston is named when the transport is constructed; another load failure verbatim (child process)', () => {
  const run = (failure) => spawnSync(process.execPath, ['-e', `
    const Module = require('node:module');
    const resolve = Module._resolveFilename;
    Module._resolveFilename = function (request, ...rest) {
      if (request === 'winston') {
        ${failure}
      }
      return resolve.call(this, request, ...rest);
    };
    const { OtelHelperTransport } = require('@staffops/otel-helper-winston');
    console.log(JSON.stringify({ loaded: typeof OtelHelperTransport }));
    try { new OtelHelperTransport(); } catch (err) { console.log(JSON.stringify({ name: err.name, message: err.message, cause: Boolean(err.cause) })); }
  `], { cwd: path.join(__dirname, '..'), encoding: 'utf8' });
  const out = (res) => res.stdout.trim().split('\n').map((l) => JSON.parse(l));
  const [loaded, missing] = out(run(`const e = new Error("Cannot find module 'winston'"); e.code = 'MODULE_NOT_FOUND'; throw e;`));
  assert.deepEqual(loaded, { loaded: 'function' });
  assert.deepEqual(missing, { name: 'Error', message: "@staffops/otel-helper-winston is installed but the library 'winston' is not. Install it (e.g. npm install winston)", cause: true });
  const [, other] = out(run(`throw new SyntaxError('Unexpected token in winston');`));
  assert.deepEqual(other, { name: 'Error', message: '@staffops/otel-helper-winston: failed to load winston: Unexpected token in winston', cause: true });
  const [, thrown] = out(run(`throw 42;`));
  assert.equal(thrown.message, '@staffops/otel-helper-winston: failed to load winston: 42');
});

// ── fix round 2026-11-03 ─────────────────────────────────────────────────

const LEVEL = Symbol.for('level');

/** A transport that calls next() later: winston buffers the next records (backpressure). */
class SlowTransport extends winston.Transport {
  log(_info, next) {
    setTimeout(next, 1);
  }
}

test('fix round HIGH-1: under backpressure every record keeps its call\'s trace (two concurrent spans), a record outside a span is uncorrelated', async () => {
  const receiver = await otlpSetup();
  let inCall = false;
  let delivered = 0;
  let deliveredLater = 0;
  class Probe extends OtelHelperTransport {
    log(info, next) {
      delivered++;
      if (!inCall) deliveredLater++;
      super.log(info, next);
    }
  }
  const ids = {};
  try {
    const log = winston.createLogger({ level: 'info', transports: [new SlowTransport({ highWaterMark: 1 }), new Probe()] });
    const call = (message, meta) => {
      inCall = true;
      try {
        log.info(message, meta);
      } finally {
        inCall = false;
      }
    };
    const tracer = api.trace.getTracer('t');
    await Promise.all(['A', 'B'].map((which) => tracer.startActiveSpan(which, async (span) => {
      ids[which] = span.spanContext();
      for (let i = 0; i < 25; i++) {
        call(`r-${which}-${i}`, { which });
        await new Promise((resolve) => setImmediate(resolve));
      }
      span.end();
    })));
    call('outside', { which: 'none' });
    const deadline = Date.now() + 20000;
    while (delivered < 51 && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  assert.equal(delivered, 51);
  assert.ok(deliveredLater > 0, 'the slow transport made winston buffer and deliver later');
  const records = receiver.logs();
  assert.equal(records.length, 51);
  const misattributed = records.filter((r) => {
    const which = r.attributes.which;
    if (which === 'none') return r.traceId !== '' || r.spanId !== '';
    return r.traceId !== ids[which].traceId || r.spanId !== ids[which].spanId;
  });
  assert.deepEqual(misattributed.map((r) => r.body), []);
  // The record time is the call's, not the (later) delivery's: in call order.
  const times = records.filter((r) => r.attributes.which === 'A').map((r) => r.timeUnixNano);
  assert.deepEqual([...times].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)), times);
});

test('fix round HIGH-1: child loggers are stamped too; a record that bypasses the logger (no stamp) is written uncorrelated, never on the active span', async () => {
  const receiver = await otlpSetup();
  let ids;
  try {
    const transport = new OtelHelperTransport();
    const log = winston.createLogger({ transports: [transport] });
    const child = log.child({ tenant: 't-1' });
    api.trace.getTracer('t').startActiveSpan('sp', (span) => {
      ids = span.spanContext();
      child.info('from-child');
      log.child({ late: true }).info('from-late-child');
      transport.log({ [LEVEL]: 'info', level: 'info', message: 'unstamped' }, () => undefined);
      span.end();
    });
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  const [fromChild, late, unstamped] = receiver.logs();
  assert.equal(fromChild.traceId, ids.traceId);
  assert.equal(fromChild.attributes.tenant, 't-1');
  assert.equal(late.traceId, ids.traceId);
  assert.equal(unstamped.body, 'unstamped');
  assert.equal(unstamped.traceId, '');
});

test('fix round: a frozen info or a frozen logger is written uncorrelated, never throws', () => {
  const transport = new OtelHelperTransport();
  transport.emit('pipe', Object.freeze({ write() {} }));
  transport.emit('pipe', null);
  transport.emit('pipe', {});
  // Logger-like sources that cannot be wrapped: not counted, never throws (fix round 2).
  transport.emit('pipe', { add() {}, write: 1 });
  transport.emit('pipe', Object.freeze({ add() {}, write() {} }));
  transport.emit('unpipe', Object.freeze({ add() {}, write() {} }));
  // A format that leaves the info as it is (winston's default json format writes into it).
  const log = winston.createLogger({ format: { transform: (info) => info }, transports: [transport] });
  transport.emit('pipe', log); // a second pipe from the same logger: wrapped once
  const lines = captureStdout(() => log.write(Object.freeze({ [LEVEL]: 'info', level: 'info', message: 'frozen-info' })));
  assert.deepEqual(lines.map((l) => l.msg), ['frozen-info']);
});

test('fix round: format.errors({ stack: true }) at the logger: the exception is rebuilt from the stack, no `stack` field', async () => {
  const err = new TypeError('flattened');
  const lines = captureStdout(() => {
    logger({ format: winston.format.errors({ stack: true }) }).error(err);
    logger().info('a stack-looking field', { stack: 'not a stack trace' });
  });
  assert.equal(lines[0].exception, err.stack);
  assert.equal(lines[0].stack, undefined);
  assert.equal(lines[1].stack, 'not a stack trace');
  assert.equal(lines[1].exception, undefined);
  const receiver = await otlpSetup();
  try {
    logger({ format: winston.format.errors({ stack: true }) }).error(err);
    // A transport-level format copies the info: the splat Error still reaches the transport.
    winston.createLogger({ transports: [new OtelHelperTransport({ format: winston.format.errors({ stack: true }) })] }).error('wrapped', err);
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  const [rebuilt, wrapped] = receiver.logs();
  assert.equal(rebuilt.attributes['exception.type'], 'TypeError');
  assert.equal(rebuilt.attributes['exception.message'], 'flattened');
  assert.equal(rebuilt.attributes['exception.stacktrace'], err.stack);
  assert.equal(rebuilt.attributes.stack, undefined);
  assert.equal(wrapped.attributes['exception.stacktrace'], err.stack);
});

test('fix round: winston\'s exception-handler info (getAllInfo): the error is the exception; error, exception, stack, trace are not fields', () => {
  const err = new RangeError('uncaught-one');
  const log = winston.createLogger({ transports: [new OtelHelperTransport()] });
  const info = new winston.ExceptionHandler(log).getAllInfo(err);
  const lines = captureStdout(() => log.transports[0].log({ ...info, [LEVEL]: 'error' }, () => undefined));
  assert.equal(lines.length, 1);
  assert.equal(lines[0].level, 'ERROR');
  assert.equal(lines[0].exception, err.stack);
  // `exception` is the stack text (the record exception), not winston's `true` flag.
  // Fix round 2 (2026-11-03): also no `date`, `process` (argv can carry secrets) or `os`.
  for (const key of ['error', 'stack', 'trace', 'date', 'process', 'os']) assert.equal(lines[0][key], undefined, key);
  // The body is the message's first line (the rest is the stack, already the exception).
  assert.equal(lines[0].msg, 'uncaughtException: uncaught-one');
});

test('fix round: non-string messages are a map body on OTel and JSON text on stdout; no message: no OTel body, msg ""', async () => {
  // `logger.info({ k: 1 })`: winston makes the object the message.
  const emit = () => {
    const log = logger();
    log.log({ level: 'info', message: { order: 'o-1', n: 2 } });
    log.info({ k: 1 });
    log.log({ level: 'info', k: 2 });
  };
  let lines = captureStdout(emit);
  assert.deepEqual(lines.map((l) => l.msg), ['{"order":"o-1","n":2}', '{"k":1}', '']);
  const receiver = await otlpSetup();
  try {
    lines = captureStdout(emit);
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  assert.deepEqual(lines, []);
  const [map, objectMessage, none] = receiver.logs();
  assert.deepEqual(map.body, { order: 'o-1', n: 2 });
  assert.deepEqual(objectMessage.body, { k: 1 });
  assert.equal(none.body, undefined);
  assert.deepEqual(none.attributes, { k: 2 });
});

test('fix round: `logged` is emitted (after the write); logger.end() flushes the helper logs pipeline before the transport finishes; close() flushes', async () => {
  const receiver = await otlpSetup();
  try {
    const transport = new OtelHelperTransport();
    const log = winston.createLogger({ transports: [transport] });
    const logged = new Promise((resolve) => transport.once('logged', resolve));
    log.warn('ended-record');
    assert.equal((await logged).message, 'ended-record');
    const finished = new Promise((resolve) => transport.once('finish', resolve));
    log.end();
    await finished;
    assert.deepEqual(receiver.logs().map((r) => r.body), ['ended-record']);
    transport.close();
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
});

test('fix round: D14 another load failure keeps only its first line, cause kept (child process)', () => {
  const res = spawnSync(process.execPath, ['-e', `
    const Module = require('node:module');
    const resolve = Module._resolveFilename;
    Module._resolveFilename = function (request, ...rest) {
      if (request === 'winston') { const e = new Error("Cannot find module 'logform'\\nRequire stack:\\n- /home/someone/app/x.js"); e.code = 'MODULE_NOT_FOUND'; throw e; }
      return resolve.call(this, request, ...rest);
    };
    const { OtelHelperTransport } = require('@staffops/otel-helper-winston');
    try { new OtelHelperTransport(); } catch (err) { console.log(JSON.stringify({ message: err.message, cause: err.cause.message })); }
  `], { cwd: path.join(__dirname, '..'), encoding: 'utf8' });
  const out = JSON.parse(res.stdout.trim());
  assert.equal(out.message, "@staffops/otel-helper-winston: failed to load winston: Cannot find module 'logform'");
  assert.match(out.cause, /Require stack/);
});

// ── fix round 2, 2026-11-03 ─────────────────────────────────────────────

const { PassThrough } = require('node:stream');

/** A second installed copy of this package (fresh module instances of its dist files). */
function secondCopy() {
  const mine = (k) => /otel-helper-winston[\\/]dist[\\/]/.test(k);
  const saved = Object.fromEntries(Object.keys(require.cache).filter(mine).map((k) => [k, require.cache[k]]));
  for (const k of Object.keys(saved)) delete require.cache[k];
  try {
    return require('@staffops/otel-helper-winston');
  } finally {
    for (const k of Object.keys(require.cache).filter(mine)) delete require.cache[k];
    Object.assign(require.cache, saved);
  }
}

test('fix round 2 HIGH-1: nothing is added to the info — a prettyPrint transport next to ours prints no symbol, before and after remove()', () => {
  const out = new PassThrough();
  let printed = '';
  out.on('data', (c) => {
    printed += String(c);
  });
  const ours = new OtelHelperTransport();
  const log = winston.createLogger({ transports: [new winston.transports.Stream({ stream: out, format: winston.format.prettyPrint() }), ours] });
  captureStdout(() => {
    log.info('with-ours', { k: 1 });
    log.remove(ours);
    log.info('without-ours', { k: 2 });
  });
  assert.match(printed, /with-ours/);
  assert.match(printed, /without-ours/);
  assert.doesNotMatch(printed, /otel-helper|Symbol\(@staffops|context/i);
});

test('fix round 2 HIGH-1: the same Error logged in two spans gives two traces and two times (the stamp is replaced per call)', async () => {
  const receiver = await otlpSetup();
  const err = new Error('reused');
  const ids = [];
  try {
    const log = winston.createLogger({ transports: [new OtelHelperTransport()] });
    const tracer = api.trace.getTracer('t');
    for (const name of ['first', 'second']) {
      tracer.startActiveSpan(name, (span) => {
        ids.push(span.spanContext().traceId);
        log.error(err);
        span.end();
      });
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  const records = receiver.logs();
  assert.deepEqual(records.map((r) => r.traceId), ids);
  assert.notEqual(ids[0], ids[1]);
  assert.ok(records[1].timeUnixNano > records[0].timeUnixNano, 'two call times');
});

test('fix round 2 HIGH-1: add, remove, add again — the logger\'s own write is restored and wrapped again; correlation works after re-adding', async () => {
  const log = winston.createLogger({ transports: [] });
  const ownWrite = log.write;
  assert.equal(Object.hasOwn(log, 'write'), false);
  const first = new OtelHelperTransport();
  const second = new OtelHelperTransport();
  log.add(first);
  log.add(second);
  assert.equal(Object.hasOwn(log, 'write'), true);
  log.remove(first);
  assert.equal(Object.hasOwn(log, 'write'), true, 'still one helper transport attached');
  log.remove(second);
  assert.equal(Object.hasOwn(log, 'write'), false);
  assert.equal(log.write, ownWrite);
  // A logger with its own `write` property gets that one back.
  const custom = winston.createLogger({ transports: [] });
  const customWrite = custom.write.bind(custom);
  Object.defineProperty(custom, 'write', { value: customWrite, writable: true, configurable: true });
  const t = new OtelHelperTransport();
  custom.add(t);
  custom.remove(t);
  assert.equal(custom.write, customWrite);
  // Something wrapping after us is left in place on removal.
  const stacked = winston.createLogger({ transports: [] });
  const t2 = new OtelHelperTransport();
  stacked.add(t2);
  const ours = stacked.write;
  const theirs = function (...args) { return ours.apply(this, args); };
  stacked.write = theirs;
  stacked.remove(t2);
  assert.equal(stacked.write, theirs);
  const receiver = await otlpSetup();
  let traceId;
  try {
    const again = new OtelHelperTransport();
    log.add(again);
    api.trace.getTracer('t').startActiveSpan('sp', (span) => {
      traceId = span.spanContext().traceId;
      log.info('after-re-add');
      span.end();
    });
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  assert.deepEqual(receiver.logs().map((r) => [r.body, r.traceId]), [['after-re-add', traceId]]);
});

test('fix round 2 HIGH-1: two installed copies of -winston on one logger both correlate, and share the per-logger count', async () => {
  const other = secondCopy();
  assert.notEqual(other.OtelHelperTransport, OtelHelperTransport);
  const receiver = await otlpSetup();
  let traceId;
  try {
    const a = new OtelHelperTransport({ name: 'copy-a' });
    const b = new other.OtelHelperTransport({ name: 'copy-b' });
    let logged = 0;
    a.on('logged', () => logged++);
    b.on('logged', () => logged++);
    const log = winston.createLogger({ transports: [a, b, new SlowTransport({ highWaterMark: 1 })] });
    api.trace.getTracer('t').startActiveSpan('sp', (span) => {
      traceId = span.spanContext().traceId;
      for (let i = 0; i < 5; i++) log.info(`r-${i}`);
      span.end();
    });
    const deadline = Date.now() + 20000;
    while (logged < 10 && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
    await helper.shutdown();
    log.remove(a);
    assert.equal(Object.hasOwn(log, 'write'), true, 'copy b still attached');
    log.remove(b);
    assert.equal(Object.hasOwn(log, 'write'), false);
  } finally {
    await receiver.close();
  }
  const records = receiver.logs();
  assert.deepEqual([...new Set(records.map((r) => r.scope))].sort(), ['copy-a', 'copy-b']);
  assert.equal(records.length, 10);
  assert.ok(records.every((r) => r.traceId === traceId), JSON.stringify(records.map((r) => [r.scope, r.body, r.traceId])));
});

test('fix round 2 HIGH-1: our transport\'s own format keeps correlation; a logger-level format that replaces the info keeps it too (final round: _transform carries the stamp)', async () => {
  const receiver = await otlpSetup();
  let traceId;
  try {
    const withOwnFormat = winston.createLogger({ transports: [new OtelHelperTransport({ name: 'own-format', format: winston.format.combine(winston.format.timestamp(), winston.format.json()) })] });
    const replacing = winston.createLogger({ format: winston.format.errors({ stack: true }), transports: [new OtelHelperTransport({ name: 'replaced' })] });
    api.trace.getTracer('t').startActiveSpan('sp', (span) => {
      traceId = span.spanContext().traceId;
      withOwnFormat.info('own-format-record');
      replacing.error(new Error('replaced-object'));
      span.end();
    });
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  const [own, replaced] = receiver.logs();
  assert.equal(own.traceId, traceId);
  assert.equal(typeof own.attributes.timestamp, 'string');
  // Flipped in the final round (2026-11-03): the object format.errors() returns gets the incoming info's stamp.
  assert.equal(replaced.traceId, traceId, 'format.errors() returned a new object: the stamp follows it');
  assert.equal(replaced.attributes['exception.message'], 'replaced-object');
});

test('fix round 2: winston\'s exception handler logs through logger.log, so its record is stamped (handleExceptions transport, in a span)', async () => {
  const receiver = await otlpSetup();
  let traceId;
  try {
    const transport = new OtelHelperTransport({ handleExceptions: true });
    const log = winston.createLogger({ exitOnError: false, transports: [transport] });
    api.trace.getTracer('t').startActiveSpan('sp', (span) => {
      traceId = span.spanContext().traceId;
      log.exceptions._uncaughtException(new TypeError('probe-q3'));
      span.end();
    });
    log.exceptions.unhandle();
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  const [record] = receiver.logs();
  assert.equal(record.body, 'uncaughtException: probe-q3');
  assert.equal(record.traceId, traceId);
  assert.equal(record.attributes['exception.type'], 'TypeError');
  for (const key of ['process', 'os', 'date', 'trace', 'error', 'stack']) assert.equal(record.attributes[key], undefined, key);
});

test('fix round 2: the stack rebuild needs a frame; coded heads `Name [CODE]: msg` and a bare `Name` (message from info.message); `code` kept', async () => {
  const frames = '\n    at fn (/app/x.js:1:1)';
  const lines = captureStdout(() => {
    const t = new OtelHelperTransport();
    const log = (info) => t.log({ [LEVEL]: 'error', level: 'error', ...info }, () => undefined);
    log({ message: 'no-frame', stack: 'Error: looks like a head' });
    log({ message: 'bad-head', stack: `not a name!${frames}` });
    log({ message: { not: 'text' }, stack: `BareName${frames}` });
    log({ message: 'coded', stack: `TypeError [ERR_INVALID_ARG_TYPE]: bad arg${frames}` });
    log({ message: 'from-info', stack: `AbortError${frames}` });
    log({ message: 'own-code', code: 'E_OWN', stack: `Error [ERR_X]: m${frames}` });
  });
  assert.equal(lines[0].stack, 'Error: looks like a head');
  assert.equal(lines[0].exception, undefined);
  assert.equal(lines[1].exception, undefined, 'a head that is not `Name…`: no rebuild');
  assert.equal(lines[1].stack, `not a name!${frames}`);
  assert.equal(lines[2].exception, `BareName${frames}`);
  assert.equal(lines[3].exception, `TypeError [ERR_INVALID_ARG_TYPE]: bad arg${frames}`);
  assert.equal(lines[3].stack, undefined);
  assert.equal(lines[4].exception, `AbortError${frames}`);
  assert.equal(lines[5].code, 'E_OWN');
  const receiver = await otlpSetup();
  try {
    const t = new OtelHelperTransport();
    const log = (info) => t.log({ [LEVEL]: 'error', level: 'error', ...info }, () => undefined);
    log({ message: 'coded', stack: `TypeError [ERR_INVALID_ARG_TYPE]: bad arg${frames}` });
    log({ message: 'from-info', stack: `AbortError${frames}` });
    log({ message: 'own-code', code: 'E_OWN', stack: `Error [ERR_X]: m${frames}` });
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  const [coded, bare, own] = receiver.logs();
  assert.equal(coded.attributes['exception.type'], 'ERR_INVALID_ARG_TYPE');
  assert.equal(coded.attributes['exception.message'], 'bad arg');
  assert.equal(bare.attributes['exception.type'], 'AbortError');
  assert.equal(bare.attributes['exception.message'], 'from-info');
  assert.equal(own.attributes['exception.type'], 'E_OWN');
});

test('fix round 2 HIGH-1: records buffered on our own transport (_writev, here by cork) keep their call\'s trace', async () => {
  const receiver = await otlpSetup();
  let traceId;
  try {
    const transport = new OtelHelperTransport();
    const log = winston.createLogger({ transports: [transport] });
    transport.cork();
    api.trace.getTracer('t').startActiveSpan('sp', (span) => {
      traceId = span.spanContext().traceId;
      log.info('corked-1');
      log.info('corked-2');
      span.end();
    });
    log.info('corked-outside');
    transport.uncork();
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  assert.deepEqual(receiver.logs().map((r) => [r.body, r.traceId]), [['corked-1', traceId], ['corked-2', traceId], ['corked-outside', '']]);
});

// ── final round, 2026-11-03 ─────────────────────────────────────────────

test('final round: logger-level format.errors() in span S, sync and buffered behind a slow transport: every record on trace S', async () => {
  const receiver = await otlpSetup();
  let traceS;
  let delivered = 0;
  try {
    const ours = new OtelHelperTransport();
    ours.on('logged', () => delivered++);
    const sync = winston.createLogger({ format: winston.format.errors({ stack: true }), transports: [new OtelHelperTransport({ name: 'sync' })] });
    const buffered = winston.createLogger({ format: winston.format.errors({ stack: true }), transports: [new SlowTransport({ highWaterMark: 1 }), ours] });
    await api.trace.getTracer('t').startActiveSpan('S', async (span) => {
      traceS = span.spanContext().traceId;
      sync.error(new Error('sync-error'));
      for (let i = 0; i < 10; i++) buffered.error(new Error(`buffered-${i}`));
      span.end();
    });
    buffered.error(new Error('outside-S'));
    const deadline = Date.now() + 20000;
    while (delivered < 11 && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
    await helper.shutdown();
  } finally {
    await receiver.close();
  }
  const records = receiver.logs();
  assert.equal(records.length, 12);
  const byMessage = Object.fromEntries(records.map((r) => [r.attributes['exception.message'], r.traceId]));
  assert.equal(byMessage['sync-error'], traceS);
  for (let i = 0; i < 10; i++) assert.equal(byMessage[`buffered-${i}`], traceS, `buffered-${i}`);
  assert.equal(byMessage['outside-S'], '');
});

test('final round: _transform is wrapped while attached and restored on detach; an own `push` is put back; an unstamped info passes through', () => {
  const log = winston.createLogger({ transports: [] });
  const ownTransform = log._transform;
  const t = new OtelHelperTransport();
  log.add(t);
  assert.equal(Object.hasOwn(log, '_transform'), true);
  // An unstamped info (never written through the wrapped write) goes straight through.
  const lines = captureStdout(() => log._transform({ [LEVEL]: 'info', level: 'info', message: 'unstamped-transform' }, 'utf8', () => undefined));
  assert.deepEqual(lines.map((l) => l.msg), ['unstamped-transform']);
  // A logger that has its own push: it is the one in place after a stamped _transform.
  const ownPush = log.push;
  Object.defineProperty(log, 'push', { value: ownPush, writable: true, configurable: true });
  captureStdout(() => log.info('with-own-push'));
  assert.equal(log.push, ownPush);
  assert.equal(Object.hasOwn(log, 'push'), true);
  log.remove(t);
  assert.equal(Object.hasOwn(log, '_transform'), false);
  assert.equal(log._transform, ownTransform);
});

// ── the shared registry (code-review L8, 2026-11-04) ───────────────────

test('registry: a foreign or unreadable value at the registry key is left alone; the transport still writes (child process)', () => {
  const key = "Symbol.for('@staffops/otel-helper-winston.registry.v1')";
  const cases = {
    foreign: `globalThis[${key}] = { stamps: 'not a WeakMap' };`,
    throwingSlot: `Object.defineProperty(globalThis, ${key}, { get() { throw new Error('slot'); } });`,
    throwingField: `Object.defineProperty(globalThis, ${key}, { value: { get stamps() { throw new Error('field'); } } });`,
    nonExtensible: 'Object.preventExtensions(globalThis);',
  };
  for (const [name, pre] of Object.entries(cases)) {
    const res = spawnSync(process.execPath, ['-e', `${pre}
      const winston = require('winston');
      const { OtelHelperTransport } = require('@staffops/otel-helper-winston');
      const log = winston.createLogger({ transports: [new OtelHelperTransport()] });
      log.warn('still-written');
    `], { cwd: path.join(__dirname, '..'), encoding: 'utf8' });
    assert.equal(res.status, 0, `${name}: ${res.stderr}`);
    assert.match(res.stdout, /still-written/, `${name}: ${res.stdout}`);
  }
});
