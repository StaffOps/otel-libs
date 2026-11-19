'use strict';
// withSpan / recordFailure / startRootSpan failure rule (task A45; contract
// R11, design D15 "Tests per language", ADR-016, ADR-014): a failure marks the
// span ERROR without a description with error.type and no exception event,
// and writes one correlated ERROR record per marked span (no deduplication,
// owner ruling 2026-11-17); cancellation stays Unset; success stays Unset, never Ok.
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('@opentelemetry/api');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { otlpReceiver } = require('../../../test/otlp-logs.cjs');

const ENV_KEYS = (k) => k.startsWith('OTEL_') || k === 'SERVICE_NAME' || k === 'ENVIRONMENT';
const TYPE = Symbol.for('otel.error_type');
const { ERROR, UNSET, OK } = api.SpanStatusCode;

let exporter;
let saved;
let diagnostics;

function baseEnv() {
  for (const k of Object.keys(process.env)) if (ENV_KEYS(k)) delete process.env[k];
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  process.env.OTEL_METRICS_EXPORTER = 'none';
}

test.beforeEach(() => {
  saved = { ...process.env };
  baseEnv();
  process.env.OTEL_LOGS_EXPORTER = 'none';
  testing.reset();
  diagnostics = [];
  testing.setDiagnosticSink((line) => diagnostics.push(line));
  exporter = new InMemorySpanExporter();
  helper.setup({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
});

test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

const spans = () => exporter.getFinishedSpans();
const byName = (name) => spans().find((s) => s.name === name);

/** Runs fn (sync or async) with process.stdout.write captured; resolves to { value | error, lines }. */
async function capture(fn) {
  const chunks = [];
  const original = process.stdout.write;
  process.stdout.write = (chunk) => {
    chunks.push(String(chunk));
    return true;
  };
  const out = {};
  try {
    out.value = await fn();
  } catch (err) {
    out.error = err;
  } finally {
    process.stdout.write = original;
  }
  // Only the helper's JSON lines: across an await the test runner may write its own (serialized) output.
  // Per write: the helper writes one whole line per call, and runner frames carry no newline we can split on.
  out.lines = chunks.flatMap((c) => c.split('\n')).filter((l) => l.startsWith('{"time":')).map((l) => JSON.parse(l));
  return out;
}
const failureLines = (lines) => lines.filter((l) => l.logger === 'otel-helper' && l.level === 'ERROR');

/** The marked-span shape every failure gets. */
function assertMarked(span, type) {
  assert.ok(span, 'span ended');
  assert.equal(span.status.code, ERROR);
  assert.equal(span.status.message, undefined, 'no description');
  assert.equal(span.attributes['error.type'], type);
  assert.deepEqual(span.events, [], 'no exception event');
}

function assertUnset(span) {
  assert.ok(span, 'span ended');
  assert.equal(span.status.code, UNSET);
  assert.equal(span.attributes['error.type'], undefined);
  assert.deepEqual(span.events, []);
}

class OrderRejectedError extends Error {
  constructor(m, options) {
    super(m, options);
    this.name = 'OrderRejectedError';
  }
}

test('D15 sync failure: ERROR, no description, error.type, no event; one stdout record with the span ids and the three attributes; same object rethrown', async () => {
  const err = new OrderRejectedError('order 42 rejected');
  let ids;
  const { error, lines } = await capture(() => helper.withSpan('op', (span) => {
    ids = span.spanContext();
    throw err;
  }));
  assert.equal(error, err, 'propagated by identity');
  assertMarked(byName('op'), 'OrderRejectedError');
  const records = failureLines(lines);
  assert.equal(records.length, 1);
  const [r] = records;
  assert.equal(r.msg, 'order 42 rejected');
  assert.equal(r.trace_id, ids.traceId);
  assert.equal(r.span_id, ids.spanId);
  assert.equal(r['exception.type'], 'OrderRejectedError');
  assert.equal(r['exception.message'], 'order 42 rejected');
  assert.equal(r['exception.stacktrace'], err.stack);
  // Key order: trace ids right after logger (A23 amendment).
  assert.deepEqual(Object.keys(r).slice(0, 6), ['time', 'level', 'msg', 'logger', 'trace_id', 'span_id']);
});

test('D15 async rejection and a throwing then: same rule; a real Promise; rejection by identity', async () => {
  const err = new TypeError('bad');
  const { error, lines } = await capture(() => helper.withSpan('a', async () => { throw err; }));
  assert.equal(error, err);
  assertMarked(byName('a'), 'TypeError');
  const thenErr = new RangeError('then broke');
  const p = helper.withSpan('t', () => ({ then() { throw thenErr; } }));
  assert.ok(p instanceof Promise);
  const second = await capture(() => p);
  assert.equal(second.error, thenErr);
  assertMarked(byName('t'), 'RangeError');
  assert.equal(failureLines(lines).length + failureLines(second.lines).length, 2);
});

test('D15 success: Unset, no record, never Ok; sync value and async value returned; child of the active span', async () => {
  const tracer = helper.getTracer('t');
  let parentId;
  const { value, lines } = await capture(() => tracer.startActiveSpan('outer', async (outer) => {
    parentId = outer.spanContext().spanId;
    const sync = helper.withSpan('sync', () => 1);
    const asyncValue = await helper.withSpan('async', async () => 2, { kind: api.SpanKind.CLIENT, attributes: { a: 1 } });
    outer.end();
    return [sync, asyncValue];
  }));
  assert.deepEqual(value, [1, 2]);
  assertUnset(byName('sync'));
  assertUnset(byName('async'));
  assert.equal(byName('sync').parentSpanContext.spanId, parentId, 'child by default');
  assert.equal(byName('async').kind, api.SpanKind.CLIENT);
  assert.deepEqual(byName('async').attributes, { a: 1 });
  assert.ok(!spans().some((s) => s.status.code === OK));
  assert.deepEqual(failureLines(lines), []);
});

test('D15 error.type: the Symbol.for("otel.error_type") override wins (own, static, prototype getter); errorType/error_type fields are ignored', async () => {
  const own = Object.assign(new Error('x'), { [TYPE]: 'demo.Rejected', errorType: 'payload', error_type: 'payload' });
  class Static extends Error {
    static get [TYPE]() { return 'static.Type'; }
  }
  class Proto extends Error {
    get [TYPE]() { return 'proto.Type'; }
  }
  const plain = Object.assign(new Error('y'), { errorType: 'payload', error_type: 'payload' });
  const empty = Object.assign(new OrderRejectedError('z'), { [TYPE]: '' });
  const { lines } = await capture(() => {
    for (const [name, err] of [['own', own], ['static', new Static('s')], ['proto', new Proto('p')], ['plain', plain], ['empty', empty]]) {
      assert.throws(() => helper.withSpan(name, () => { throw err; }), (e) => e === err);
    }
  });
  assert.equal(byName('own').attributes['error.type'], 'demo.Rejected');
  assert.equal(byName('static').attributes['error.type'], 'static.Type', 'a static member of the class');
  assert.equal(byName('proto').attributes['error.type'], 'proto.Type');
  assert.equal(byName('plain').attributes['error.type'], 'Error');
  assert.equal(byName('empty').attributes['error.type'], 'OrderRejectedError', 'an empty override is ignored');
  assert.deepEqual(failureLines(lines).map((l) => l['exception.type']), ['demo.Rejected', 'static.Type', 'proto.Type', 'Error', 'OrderRejectedError']);
});

test('D15 error.type defaults: name unless "Error", else constructor.name, else "Error"; a non-Error value is "Error" with its string body', async () => {
  class Unnamed extends Error {}
  const anonymous = new (class extends Error {})('a');
  const nullProto = Object.setPrototypeOf(new Error('n'), null);
  const { lines } = await capture(() => {
    for (const [name, err] of [['named', new OrderRejectedError('m')], ['unnamed', new Unnamed('u')], ['anon', anonymous], ['plain', new Error('e')], ['str', 'boom'], ['num', 7], ['obj', { code: 'E1' }], ['nullproto', nullProto]]) {
      assert.throws(() => helper.withSpan(name, () => { throw err; }), (e) => e === err);
    }
  });
  assert.deepEqual(['named', 'unnamed', 'anon', 'plain', 'str', 'num', 'obj', 'nullproto'].map((n) => byName(n).attributes['error.type']),
    ['OrderRejectedError', 'Unnamed', 'Error', 'Error', 'Error', 'Error', 'Error', 'Error']);
  const records = failureLines(lines);
  assert.deepEqual(records.map((r) => r.msg), ['m', 'u', 'a', 'e', 'boom', '7', '[object Object]', '[object Error]']);
  assert.equal(records[4]['exception.stacktrace'], undefined, 'a primitive has no stack');
  assert.equal(records[4]['exception.message'], 'boom');
});

test('D15 cancellation: AbortError and a grpc-js CANCELLED ServiceError leave the span Unset with no record; TimeoutError and code 1 without metadata are failures', async () => {
  const controller = new AbortController();
  controller.abort();
  const abortErr = controller.signal.reason;
  assert.equal(abortErr.name, 'AbortError');
  const grpcCancelled = Object.assign(new Error('1 CANCELLED: Cancelled on client'), { code: 1, details: 'Cancelled on client', metadata: {} });
  const plainAbort = Object.assign(new Error('aborted'), { name: 'AbortError' });
  const timeout = AbortSignal.timeout(1);
  await new Promise((r) => setTimeout(r, 20));
  const timeoutErr = timeout.reason;
  assert.equal(timeoutErr.name, 'TimeoutError');
  const codeOnly = Object.assign(new Error('code 1, not gRPC'), { code: 1 });
  const wrappedAbort = new Error('wrapped', { cause: abortErr });
  const { lines } = await capture(async () => {
    for (const [name, err] of [['abort', abortErr], ['grpc', grpcCancelled], ['plain-abort', plainAbort]]) {
      await assert.rejects(helper.withSpan(name, async () => { throw err; }), (e) => e === err);
    }
    for (const [name, err] of [['timeout', timeoutErr], ['code-only', codeOnly], ['wrapped', wrappedAbort]]) {
      await assert.rejects(helper.withSpan(name, async () => { throw err; }), (e) => e === err);
    }
  });
  for (const name of ['abort', 'grpc', 'plain-abort']) assertUnset(byName(name));
  assertMarked(byName('timeout'), 'TimeoutError');
  assertMarked(byName('code-only'), 'Error');
  assertMarked(byName('wrapped'), 'Error');
  assert.deepEqual(failureLines(lines).map((l) => l.msg), [timeoutErr.message, 'code 1, not gRPC', 'wrapped']);
});

test('D15 nesting (owner ruling 2026-11-17): a failure crossing 3 helper spans marks all three, and each writes its own record with its own span id', async () => {
  const err = new OrderRejectedError('nested');
  const ids = {};
  const { error, lines } = await capture(() => helper.withSpan('a', (a) => {
    ids.a = a.spanContext().spanId;
    return helper.withSpan('b', (b) => {
      ids.b = b.spanContext().spanId;
      return helper.withSpan('c', async (c) => {
        ids.c = c.spanContext().spanId;
        throw err;
      });
    });
  }));
  assert.equal(error, err);
  for (const n of ['a', 'b', 'c']) assertMarked(byName(n), 'OrderRejectedError');
  const records = failureLines(lines);
  assert.deepEqual(records.map((r) => r.span_id), [ids.c, ids.b, ids.a], 'innermost first, one per span');
  assert.ok(records.every((r) => r.msg === 'nested' && r.trace_id === byName('a').spanContext().traceId));
});

test('D15 AbortError caused by a TimeoutError (Node core API given AbortSignal.timeout()) is a failure with error.type TimeoutError', async () => {
  const { setTimeout: sleep } = require('node:timers/promises');
  // A ref'd timer keeps the event loop alive (AbortSignal.timeout's own timer is unref'd), so beforeExit cannot fire.
  const keepAlive = setInterval(() => undefined, 1000);
  let caught;
  let lines;
  try {
    ({ error: caught, lines } = await capture(() => helper.withSpan('timed', () => sleep(10_000, undefined, { signal: AbortSignal.timeout(10) }))));
  } finally {
    clearInterval(keepAlive);
  }
  assert.equal(caught.name, 'AbortError', 'Node rejects with AbortError');
  assert.equal(caught.cause?.name, 'TimeoutError');
  assertMarked(byName('timed'), 'TimeoutError');
  const [r] = failureLines(lines);
  assert.equal(r['exception.type'], 'TimeoutError');
  assert.equal(r.msg, caught.message);
  // A hand-made AbortError with a TimeoutError cause: the same; an override still wins; a plain AbortError cause stays a cancellation.
  const manual = Object.assign(new Error('aborted'), { name: 'AbortError', cause: Object.assign(new Error('t'), { name: 'TimeoutError' }) });
  const overridden = Object.assign(new Error('aborted'), { name: 'AbortError', cause: manual.cause, [TYPE]: 'custom.Timeout' });
  const abortCause = Object.assign(new Error('aborted'), { name: 'AbortError', cause: Object.assign(new Error('a'), { name: 'AbortError' }) });
  const more = await capture(() => {
    for (const [name, e] of [['manual', manual], ['overridden', overridden], ['abort-cause', abortCause]]) {
      assert.throws(() => helper.withSpan(name, () => { throw e; }), (x) => x === e);
    }
  });
  assertMarked(byName('manual'), 'TimeoutError');
  assertMarked(byName('overridden'), 'custom.Timeout');
  assertUnset(byName('abort-cause'));
  assert.deepEqual(failureLines(more.lines).map((l) => l['exception.type']), ['TimeoutError', 'custom.Timeout']);
});

test('D15 a primitive and a new root span: every marked span writes its own record', async () => {
  const err = new Error('shared');
  const first = await capture(() => helper.withSpan('outer', () => helper.startRootSpan('root', () => { throw err; })));
  assert.equal(first.error, err);
  assert.notEqual(byName('outer').spanContext().traceId, byName('root').spanContext().traceId);
  assert.deepEqual(failureLines(first.lines).map((r) => r.span_id), [byName('root').spanContext().spanId, byName('outer').spanContext().spanId]);
  const prim = await capture(() => helper.withSpan('p-outer', () => helper.withSpan('p-inner', () => { throw 'primitive'; })));
  assert.equal(prim.error, 'primitive');
  assert.equal(failureLines(prim.lines).length, 2);
});

test('D15 hostile cause, name, message and stack getters and a hostile Proxy still mark the span and give a record', async () => {
  const err = new Error('x');
  Object.defineProperty(err, 'cause', { get() { throw new Error('cause getter'); } });
  Object.defineProperty(err, 'name', { get() { throw new Error('name getter'); } });
  Object.defineProperty(err, 'message', { get() { throw new Error('message getter'); } });
  Object.defineProperty(err, 'stack', { get() { throw new Error('stack getter'); } });
  const hostile = new Proxy({}, { get() { throw new Error('trap'); }, has() { throw new Error('has'); } });
  const nonString = Object.assign(new Error('ignored'), { message: 42 });
  const { lines } = await capture(() => {
    assert.throws(() => helper.withSpan('g', () => { throw err; }), (e) => e === err);
    assert.throws(() => helper.withSpan('h', () => { throw hostile; }), (e) => e === hostile);
    assert.throws(() => helper.withSpan('n', () => { throw nonString; }), (e) => e === nonString);
  });
  assertMarked(byName('g'), 'Error');
  assertMarked(byName('h'), 'Error');
  const records = failureLines(lines);
  assert.deepEqual(records.map((r) => r.msg), ['', 'unknown error', '42']);
  assert.equal(records[0]['exception.stacktrace'], undefined);
});

test('D15 recordFailure: default = the active span (not ended); explicit non-current span → its ids; no span → record only; invalid span → TypeError', async () => {
  const tracer = helper.getTracer('t');
  const handled = new OrderRejectedError('handled');
  let activeIds;
  let otherIds;
  const { lines } = await capture(() => {
    tracer.startActiveSpan('active', (span) => {
      activeIds = span.spanContext();
      helper.recordFailure(handled);
      assert.equal(span.isRecording(), true, 'not ended');
      span.end();
    });
    const other = tracer.startSpan('other');
    otherIds = other.spanContext();
    tracer.startActiveSpan('current', (current) => {
      helper.recordFailure(new Error('for other'), other);
      current.end();
    });
    other.end();
    helper.recordFailure(new Error('no span'));
  });
  assertMarked(byName('active'), 'OrderRejectedError');
  assertMarked(byName('other'), 'Error');
  assertUnset(byName('current'));
  const records = failureLines(lines);
  assert.deepEqual(records.map((r) => [r.msg, r.span_id]), [['handled', activeIds.spanId], ['for other', otherIds.spanId], ['no span', undefined]]);
  assert.equal(records[1].trace_id, otherIds.traceId);
  assert.throws(() => helper.recordFailure(new Error('x'), 'not a span'), TypeError);
  assert.throws(() => helper.recordFailure(new Error('x'), {}), /span must be a Span/);
  // R8: no failure value at all is a programming error (as Python).
  assert.throws(() => helper.recordFailure(undefined), /recordFailure: err must not be undefined/);
  assert.throws(() => helper.recordFailure(null), /recordFailure: err must not be null/);
  assert.throws(() => helper.recordFailure(), TypeError);
  assert.equal(records.length, 3, 'nothing recorded for the rejected calls');
});

test('D15 recordFailure always records: recordFailure then a rethrow through withSpan gives two records; Ok set earlier stays Ok, error.type and the record are added', async () => {
  const err = new Error('once');
  const { lines } = await capture(() => {
    assert.throws(() => helper.withSpan('w', () => {
      helper.recordFailure(err);
      throw err;
    }), (e) => e === err);
    helper.withSpan('ok', (span) => {
      span.setStatus({ code: OK });
      helper.recordFailure(new Error('after ok'));
    });
  });
  assertMarked(byName('w'), 'Error');
  assert.equal(byName('ok').status.code, OK, 'Ok is final');
  assert.equal(byName('ok').attributes['error.type'], 'Error');
  assert.deepEqual(failureLines(lines).map((l) => l.msg), ['once', 'once', 'after ok']);
});

test('D15 unsampled parent: the span is not recording, the record is still written with its ids', async () => {
  const parent = api.trace.setSpanContext(api.context.active(), { traceId: '0af7651916cd43dd8448eb211c80319c', spanId: 'b7ad6b7169203331', traceFlags: 0, isRemote: true });
  let recording;
  const { lines } = await capture(() => api.context.with(parent, () => {
    try {
      helper.withSpan('unsampled', (span) => {
        recording = span.isRecording();
        throw new Error('dropped span');
      });
    } catch {
      // expected
    }
  }));
  assert.equal(recording, false);
  assert.equal(byName('unsampled'), undefined, 'not exported');
  const [r] = failureLines(lines);
  assert.equal(r.msg, 'dropped span');
  assert.equal(r.trace_id, '0af7651916cd43dd8448eb211c80319c');
  assert.match(r.span_id, /^[0-9a-f]{16}$/);
  assert.notEqual(r.span_id, 'b7ad6b7169203331', 'its own span id, not the parent');
});

test('D15 span marking does not depend on the log pipeline: a throwing stdout still marks the span; the write failure is reported once', async () => {
  const err = new Error('no pipeline');
  const original = process.stdout.write;
  process.stdout.write = () => { throw new Error('EPIPE'); };
  try {
    assert.throws(() => helper.withSpan('m', () => { throw err; }), (e) => e === err);
  } finally {
    process.stdout.write = original;
  }
  assertMarked(byName('m'), 'Error');
  assert.ok(diagnostics.some((l) => /could not be written/.test(l)), diagnostics.join('\n'));
});

test('D15 a fake span whose methods throw: the error is rethrown, the span ended once', async () => {
  const err = new Error('orig');
  let ended = 0;
  const span = {
    setStatus() { throw new Error('status'); },
    setAttribute() { throw new Error('attr'); },
    spanContext() { throw new Error('ctx'); },
    end() { ended++; throw new Error('end'); },
  };
  const tracer = { startActiveSpan: (_n, _o, _c, fn) => fn(span) };
  const { error } = await capture(() => helper.withSpan('x', async () => { throw err; }, { tracer }));
  assert.equal(error, err);
  assert.equal(ended, 1);
  const ok = await capture(() => helper.withSpan('y', () => 'v', { tracer }));
  assert.equal(ok.value, 'v');
});

test('D15 startRootSpan follows the rule: no message, stack, description or event on the span; one record; a new trace', async () => {
  const err = new OrderRejectedError('declined');
  const { error, lines } = await capture(() => helper.withSpan('outer', () => helper.startRootSpan('root', () => { throw err; })));
  assert.equal(error, err);
  const root = byName('root');
  assertMarked(root, 'OrderRejectedError');
  assert.equal(root.parentSpanContext, undefined);
  assert.equal(failureLines(lines).filter((l) => l.trace_id === root.spanContext().traceId).length, 1);
});

test('withSpan: root option starts a new trace; validation fails fast (name, fn, options)', async () => {
  helper.getTracer().startActiveSpan('outer', (outer) => {
    helper.withSpan('rooted', () => undefined, { root: true });
    outer.end();
  });
  assert.notEqual(byName('rooted').spanContext().traceId, byName('outer').spanContext().traceId);
  assert.throws(() => helper.withSpan(1, () => 1), /withSpan: name must be a string/);
  assert.throws(() => helper.withSpan('x', 'nope'), /withSpan: fn must be a function/);
  assert.throws(() => helper.withSpan('x', () => 1, null), /withSpan: options must be an object/);
  assert.throws(() => helper.startRootSpan('x', () => 1, 'bad'), /startRootSpan: options must be an object/);
  for (const bad of ['yes', 1, null, {}]) {
    assert.throws(() => helper.withSpan('x', () => 1, { root: bad }), (e) => e instanceof TypeError && /withSpan: root must be a boolean/.test(e.message), String(bad));
    assert.throws(() => helper.startRootSpan('x', () => 1, { root: bad }), /startRootSpan: root must be a boolean/);
  }
  helper.withSpan('explicit-false', () => undefined, { root: false });
  assert.ok(byName('explicit-false'));
  assert.ok(!spans().some((s) => s.name === 'x'), 'nothing started for invalid input');
});

test('A23 stdout ids: a record in a valid span context carries trace_id/span_id after logger, a same-named attribute is dropped; none outside a span', async () => {
  const logger = helper.createLogger({ name: 'app' });
  let ids;
  const { lines } = await capture(() => {
    helper.getTracer().startActiveSpan('s', (span) => {
      ids = span.spanContext();
      logger.warn('in-span', { trace_id: 'spoof', span_id: 'spoof', other: 1 });
      span.end();
    });
    logger.warn('outside', { trace_id: 'kept' });
    api.context.with(api.trace.setSpanContext(api.context.active(), api.INVALID_SPAN_CONTEXT), () => logger.warn('invalid'));
  });
  assert.deepEqual(lines[0], { time: lines[0].time, level: 'WARN', msg: 'in-span', logger: 'app', trace_id: ids.traceId, span_id: ids.spanId, other: 1 });
  assert.deepEqual(Object.keys(lines[0]), ['time', 'level', 'msg', 'logger', 'trace_id', 'span_id', 'other']);
  assert.equal(lines[1].trace_id, 'kept', 'no span context: the attribute is kept');
  assert.equal(lines[1].span_id, undefined);
  assert.equal(lines[2].trace_id, undefined);
});

test('D15 OTLP logs path: the record has severity ERROR, event name exception, the body, the three attributes (name, not code), the span ids; never stdout', async () => {
  await helper.shutdown();
  testing.reset();
  testing.setDiagnosticSink(() => undefined);
  baseEnv();
  const receiver = await otlpReceiver();
  try {
    process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'none';
    exporter = new InMemorySpanExporter();
    helper.setup({ otelEndpoint: receiver.endpoint, otlpProtocol: 'http/protobuf', spanProcessors: [new SimpleSpanProcessor({ exporter })] });
    const err = Object.assign(new OrderRejectedError('14 UNAVAILABLE: no'), { code: 14 });
    let ids;
    const { error, lines } = await capture(() => helper.withSpan('otlp', async (span) => {
      ids = span.spanContext();
      throw err;
    }));
    assert.equal(error, err);
    // Before shutdown: shutting down the in-memory exporter clears its spans.
    assertMarked(byName('otlp'), 'OrderRejectedError');
    await helper.shutdown();
    assert.deepEqual(lines, [], 'nothing on stdout');
    const records = receiver.logs().filter((r) => r.scope === 'otel-helper');
    assert.equal(records.length, 1);
    const [r] = records;
    assert.equal(r.severityNumber, api.SeverityNumber.ERROR);
    assert.equal(r.severityText, 'ERROR');
    assert.equal(r.eventName, 'exception');
    assert.equal(r.body, '14 UNAVAILABLE: no');
    assert.equal(r.attributes['exception.type'], 'OrderRejectedError', 'not the code');
    assert.equal(r.attributes['exception.message'], '14 UNAVAILABLE: no');
    assert.equal(r.attributes['exception.stacktrace'], err.stack);
    assert.equal(r.traceId, ids.traceId);
    assert.equal(r.spanId, ids.spanId);
  } finally {
    await receiver.close();
  }
});
