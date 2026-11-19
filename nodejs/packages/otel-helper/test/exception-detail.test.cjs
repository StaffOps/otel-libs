'use strict';
// HTTP, gRPC and framework exception detail (task A37, ADR-014 decision 4, contract design D15
// amendment 2026-11-19): server spans (http SERVER, express, grpc SERVER) keep a type-only
// exception event, no description and write one correlated ERROR record, sampled or not, none for a
// cancellation; client spans (http CLIENT, undici, grpc CLIENT) keep a type-only event, no description,
// no grpc.error_message and write no record.

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const api = require('@opentelemetry/api');
const { InMemorySpanExporter, SimpleSpanProcessor, TracerProvider, AlwaysOffSampler } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const detail = require('../dist/internal/exception-detail.js');

const { ERROR, UNSET } = api.SpanStatusCode;
const { SERVER, CLIENT, INTERNAL } = api.SpanKind;
const SECRET = 'bound-secret-4711';

let exporter;
let saved;

function baseEnv() {
  for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_') || k === 'SERVICE_NAME' || k === 'ENVIRONMENT') delete process.env[k];
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  process.env.OTEL_METRICS_EXPORTER = 'none';
  process.env.OTEL_LOGS_EXPORTER = 'none';
}

function setup(env = {}) {
  Object.assign(process.env, env);
  exporter = new InMemorySpanExporter();
  helper.setup({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
}

test.beforeEach(() => {
  saved = { ...process.env };
  baseEnv();
  testing.reset();
  testing.setDiagnosticSink(() => undefined);
});

test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

/** Runs fn with process.stdout captured; resolves to the helper's failure records (stdout JSON lines). */
async function records(fn) {
  const chunks = [];
  const original = process.stdout.write;
  process.stdout.write = (chunk, ...rest) => {
    const text = String(chunk);
    if (text.startsWith('{"time":')) {
      chunks.push(text);
      return true;
    }
    return original.call(process.stdout, chunk, ...rest);
  };
  try {
    await fn();
  } finally {
    process.stdout.write = original;
  }
  return chunks
    .flatMap((c) => c.split('\n'))
    .filter((l) => l.startsWith('{"time":'))
    .map((l) => JSON.parse(l))
    .filter((l) => l.logger === 'otel-helper' && l.level === 'ERROR');
}

/** No message or stack on the span: description, event attributes, attributes. */
function assertNoDetail(span) {
  assert.equal(span.status.message, undefined, `description of ${span.name}`);
  for (const e of span.events) assert.deepEqual(Object.keys(e.attributes ?? {}), ['exception.type'], `event of ${span.name}`);
  assert.equal(span.attributes['grpc.error_message'], undefined);
  const kept = Object.fromEntries(Object.entries(span.attributes).filter(([k]) => !['url.full', 'http.url', 'http.target', 'url.path'].includes(k)));
  assert.ok(!JSON.stringify([span.events, kept]).includes(SECRET), `${span.name} carries the message`);
}

const exceptionTypes = (span) => span.events.filter((e) => e.name === 'exception').map((e) => e.attributes['exception.type']);
const hex = (ctx) => ({ trace_id: ctx.traceId, span_id: ctx.spanId });
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(pred, ms = 5000) {
  const deadline = Date.now() + ms;
  while (!pred() && Date.now() < deadline) await wait(20);
}

// ── unit: roles and span patch ──────────────────────────────────────────

test('A37 roles: http/grpc by kind, express always, undici never; other names untouched', () => {
  assert.equal(detail.serverRole('@opentelemetry/instrumentation-http'), 'kind');
  assert.equal(detail.serverRole('@opentelemetry/instrumentation-grpc'), 'kind');
  assert.equal(detail.serverRole('@opentelemetry/instrumentation-express'), 'always');
  assert.equal(detail.serverRole('@opentelemetry/instrumentation-undici'), 'never');
  assert.equal(detail.serverRole('@opentelemetry/instrumentation-pg'), undefined);
  assert.equal(detail.serverRole('toString'), undefined);
  const provider = new TracerProvider();
  assert.equal(detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-pg'), provider);
  assert.notEqual(detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-http'), provider);
});

function sdk(sampler) {
  const ex = new InMemorySpanExporter();
  const provider = new TracerProvider({ sampler, spanProcessors: [new SimpleSpanProcessor({ exporter: ex })] });
  return { ex, provider };
}

test('A37 server span: type-only event once per object, no description, one record with the span ids and http attributes', async () => {
  setup();
  const { ex, provider } = sdk();
  const tracer = detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-http').getTracer('t');
  let span;
  const out = await records(() => {
    span = tracer.startSpan('POST', { kind: SERVER, attributes: { 'http.request.method': 'POST', 'url.full': 'http://h/p?q=1' } });
    const err = new TypeError(`order ${SECRET} rejected`);
    span.setAttribute('error.type', 'TypeError');
    span.setStatus({ code: ERROR, message: err.message });
    span.recordException(err);
    span.recordException(err);
    span.recordException(new RangeError('second'));
    span.setAttributes({ 'http.route': '/orders', 'http.response.status_code': 500 });
    span.end();
    span.end();
  });
  const [s] = ex.getFinishedSpans();
  assert.equal(s.status.code, ERROR);
  assertNoDetail(s);
  assert.deepEqual(exceptionTypes(s), ['TypeError', 'RangeError']);
  assert.equal(out.length, 1);
  const r = out[0];
  assert.equal(r.msg, `order ${SECRET} rejected`);
  assert.deepEqual({ trace_id: r.trace_id, span_id: r.span_id }, hex(s.spanContext()));
  assert.equal(r['exception.type'], 'TypeError');
  assert.match(r['exception.stacktrace'], /exception-detail\.test\.cjs/);
  assert.equal(r['error.type'], '500', 'R2: the response status');
  assert.equal(r['http.request.method'], 'POST');
  assert.equal(r['http.route'], '/orders');
  assert.equal(r['http.response.status_code'], 500, 'the attribute type is kept');
  assert.equal(r['url.full'], undefined);
});

test('A37 client span: type-only event, no description, no record; OK status passes through', async () => {
  setup();
  const { ex, provider } = sdk();
  const tracer = detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-undici').getTracer('t');
  const out = await records(() => {
    const span = tracer.startSpan('GET', { kind: CLIENT });
    span.recordException(new Error(`refused ${SECRET}`));
    span.setStatus({ code: ERROR, message: `refused ${SECRET}` });
    span.end();
    const ok = tracer.startSpan('ok', { kind: CLIENT });
    ok.setStatus({ code: api.SpanStatusCode.OK });
    ok.end();
  });
  const byName = Object.fromEntries(ex.getFinishedSpans().map((s) => [s.name, s]));
  assertNoDetail(byName.GET);
  assert.deepEqual(exceptionTypes(byName.GET), ['Error']);
  assert.equal(byName.ok.status.code, api.SpanStatusCode.OK);
  assert.deepEqual(out, []);
});

for (const sampler of [undefined, new AlwaysOffSampler()]) {
  test(`A37 gRPC server records follow the code (R1, R2): server-error codes and no code write, others do not (${sampler ? 'unsampled' : 'sampled'})`, async () => {
    setup();
    const { ex, provider } = sdk(sampler);
    const tracer = detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-grpc').getTracer('t');
    const call = (name, attrs) => {
      const span = tracer.startSpan(name, { kind: SERVER, attributes: { 'rpc.system': 'grpc', 'rpc.grpc.status_code': 0 } });
      span.setAttribute('grpc.error_message', `${name} ${SECRET}`);
      span.setAttributes({ 'grpc.error_name': 'Error', ...attrs });
      span.end();
    };
    const out = await records(() => {
      call('svc/Internal', { 'rpc.grpc.status_code': 13 });
      call('svc/NoCode', {});
      call('svc/NotFound', { 'rpc.grpc.status_code': 5 });
      call('svc/Cancelled', { 'rpc.grpc.status_code': 1 });
      call('svc/Precondition', { 'rpc.grpc.status_code': 9 });
      const plain = tracer.startSpan('svc/Plain', { kind: SERVER });
      plain.setStatus({ code: ERROR });
      plain.end();
    });
    for (const s of ex.getFinishedSpans()) assertNoDetail(s);
    assert.deepEqual(out.map((r) => [r.msg, r['error.type'], r['rpc.grpc.status_code']]), [
      [`svc/Internal ${SECRET}`, 'INTERNAL', 13],
      [`svc/NoCode ${SECRET}`, 'UNKNOWN', undefined],
    ]);
    assert.equal(out[0]['exception.type'], 'Error');
    assert.equal(out[0]['exception.stacktrace'], undefined);
    assert.equal(out[0]['rpc.system'], 'grpc');
  });
}

test('A37 http SERVER listener failure without a status: error.type is the span value (err.name)', async () => {
  setup();
  const { provider } = sdk(new AlwaysOffSampler());
  const tracer = detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-http').getTracer('t');
  const out = await records(() => {
    const span = tracer.startSpan('POST', { kind: SERVER });
    const err = new RangeError(`listener ${SECRET}`);
    span.setAttribute('error.type', err.name); // setSpanWithError
    span.setStatus({ code: ERROR, message: err.message });
    span.recordException(err);
    span.end();
  });
  assert.equal(out.length, 1);
  assert.equal(out[0]['error.type'], 'RangeError');
});

test('A37 a server span whose exception the app handled (no ERROR status) writes no record', async () => {
  setup();
  const { provider } = sdk();
  const tracer = detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-http').getTracer('t');
  const out = await records(() => {
    const span = tracer.startSpan('GET', { kind: SERVER });
    span.recordException(new Error(SECRET));
    span.setAttributes({ 'http.response.status_code': 200 });
    span.end();
  });
  assert.deepEqual(out, []);
});

test('A37 internal failures are reported once, by type only', async () => {
  setup();
  const lines = [];
  testing.setDiagnosticSink((line) => lines.push(line));
  const { provider } = sdk();
  const tracer = detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-express').getTracer('t');
  const hostile = new Proxy(new Error(SECRET), { get(t, k) { if (k === 'name' || k === 'message' || k === 'stack') throw new RangeError(SECRET); return Reflect.get(t, k); } });
  await records(() => {
    for (let i = 0; i < 2; i++) {
      const span = tracer.startSpan('request handler - /x');
      span.recordException(hostile);
      span.setStatus({ code: ERROR });
      span.end();
    }
  });
  assert.ok(!lines.join('\n').includes(SECRET), lines.join('\n'));
});

test('A37 cancellation on a server span: the type-only event stays, no record', async () => {
  setup();
  const { ex, provider } = sdk();
  const tracer = detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-express').getTracer('t');
  const out = await records(() => {
    const span = tracer.startSpan('request handler - /x');
    const abort = new Error('aborted');
    abort.name = 'AbortError';
    span.recordException(abort);
    span.end();
  });
  assert.deepEqual(exceptionTypes(ex.getFinishedSpans()[0]), ['AbortError']);
  assert.deepEqual(out, []);
});

test('A37 unsampled server span: the instrumentations call the API anyway, the record carries the unsampled ids', async () => {
  setup();
  const { ex, provider } = sdk(new AlwaysOffSampler());
  const tracer = detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-express').getTracer('t');
  let ctx;
  const out = await records(() =>
    tracer.startActiveSpan('request handler - /x', { kind: INTERNAL, attributes: { 'http.route': '/x' } }, (span) => {
      ctx = span.spanContext();
      span.recordException(new Error(`unsampled ${SECRET}`));
      span.setStatus({ code: ERROR, message: 'x' });
      span.end();
    }),
  );
  assert.deepEqual(ex.getFinishedSpans(), []);
  assert.equal(out.length, 1);
  assert.deepEqual({ trace_id: out[0].trace_id, span_id: out[0].span_id }, hex(ctx));
  assert.equal(ctx.traceFlags & api.TraceFlags.SAMPLED, 0);
  assert.equal(out[0]['http.route'], '/x');
  assert.equal(out[0]['error.type'], 'Error', 'N6: no status known on the framework span: the exception type');
});

test('A37 startActiveSpan forms (name, fn) and (name, options, context, fn) are patched; a frozen span is left alone', async () => {
  setup();
  const { ex, provider } = sdk();
  const tracer = detail.exceptionDetailTracerProvider(provider, '@opentelemetry/instrumentation-http').getTracer('t');
  tracer.startActiveSpan('a', (span) => {
    span.setStatus({ code: ERROR, message: SECRET });
    span.end();
  });
  tracer.startActiveSpan('b', { kind: CLIENT }, api.context.active(), (span) => {
    span.setStatus({ code: ERROR, message: SECRET });
    span.end();
  });
  for (const s of ex.getFinishedSpans()) assert.equal(s.status.message, undefined, s.name);
  const frozen = Object.freeze({ setStatus() {}, setAttribute() {}, setAttributes() {}, addEvent() {}, end() {}, recordException() {} });
  assert.equal(detail.applyExceptionDetail(frozen, true), frozen);
});

// ── real instrumentations ───────────────────────────────────────────────

function listen(server) {
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
}

function post(port, path) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path, method: 'POST', agent: false }, (res) => {
      res.resume();
      res.on('end', () => resolve(res.statusCode));
    });
    req.on('error', reject);
    req.end();
  });
}

async function expressFailure(env) {
  setup(env);
  const express = require('express');
  const app = express();
  app.post('/orders/:id', (req) => {
    throw new TypeError(`order ${req.params.id} ${SECRET}`);
  });
  app.post('/missing', () => {
    throw Object.assign(new Error(`missing ${SECRET}`), { status: 404 });
  });
  // eslint-disable-next-line no-unused-vars
  app.use((err, _req, res, _next) => res.status(err.status ?? 500).end('failed'));
  const server = http.createServer(app);
  const port = await listen(server);
  let status;
  let missing;
  const out = await records(async () => {
    missing = await post(port, '/missing');
    status = await post(port, '/orders/42');
    await until(() => env.OTEL_TRACES_SAMPLER || exporter.getFinishedSpans().some((s) => s.kind === SERVER));
    await wait(50);
  });
  server.close();
  assert.equal(status, 500);
  assert.equal(missing, 404);
  return out;
}

test('A37 express: the request handler span keeps a type-only event and writes the one record; the http SERVER span writes none', async () => {
  const out = await expressFailure({});
  const finished = exporter.getFinishedSpans();
  for (const s of finished) assertNoDetail(s);
  const handler = finished.find((s) => s.name.startsWith('request handler') && s.name.includes('orders'));
  assert.ok(handler, finished.map((s) => s.name).join());
  assert.deepEqual(exceptionTypes(handler), ['TypeError']);
  assert.equal(handler.status.code, ERROR);
  assert.equal(out.length, 1, JSON.stringify(out)); // the 404 error (status 404) writes none
  assert.equal(out[0].span_id, handler.spanContext().spanId);
  assert.equal(out[0].msg, `order 42 ${SECRET}`);
  assert.equal(out[0]['exception.type'], 'TypeError');
  assert.equal(out[0]['error.type'], 'TypeError', 'N6: no status on the express span');
});

test('A37 express unsampled (OTEL_TRACES_SAMPLER=always_off): still one record, with valid ids', async () => {
  const out = await expressFailure({ OTEL_TRACES_SAMPLER: 'always_off' });
  assert.deepEqual(exporter.getFinishedSpans(), []);
  assert.equal(out.length, 1, JSON.stringify(out));
  assert.match(out[0].trace_id, /^[0-9a-f]{32}$/);
  assert.match(out[0].span_id, /^[0-9a-f]{16}$/);
  assert.equal(out[0].msg, `order 42 ${SECRET}`);
  assert.equal(out[0]['error.type'], 'TypeError', 'R2: the same value as sampled');
});

test('A37 http and undici client failures: type-only event, no description, no record', async () => {
  setup();
  // A server that drops the connection: both clients fail after the request was sent.
  const server = http.createServer((req) => req.socket.destroy());
  const port = await listen(server);
  const out = await records(async () => {
    await new Promise((resolve) => {
      const req = http.request(`http://127.0.0.1:${port}/p?q=${SECRET}`, { agent: false }, () => resolve());
      req.on('error', () => resolve());
      req.end();
    });
    await fetch(`http://127.0.0.1:${port}/p?q=${SECRET}`).catch(() => undefined);
    await until(() => exporter.getFinishedSpans().filter((s) => s.kind === CLIENT).length >= 2);
  });
  server.close();
  const clients = exporter.getFinishedSpans().filter((s) => s.kind === CLIENT);
  assert.equal(clients.length, 2, clients.map((s) => s.name).join());
  for (const s of clients) {
    assert.equal(s.status.code, ERROR, s.name);
    assertNoDetail(s);
    assert.equal(exceptionTypes(s).length, 1, s.name);
  }
  assert.deepEqual(out, []);
});

test('A37 real grpc-js: server error text leaves no description and no grpc.error_message; one record for INTERNAL; none for NOT_FOUND, the client or CANCELLED', async () => {
  setup();
  const grpc = require('@grpc/grpc-js');
  const identity = (b) => b;
  const P = '/a37.v1.Svc/';
  const def = (path) => ({ path, requestStream: false, responseStream: false, requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity });
  const server = new grpc.Server();
  server.addService(
    { Internal: def(`${P}Internal`), NotFound: def(`${P}NotFound`), Slow: def(`${P}Slow`) },
    {
      Internal: (_call, cb) => cb(Object.assign(new Error(`internal ${SECRET}`), { code: grpc.status.INTERNAL })),
      NotFound: (_call, cb) => cb(Object.assign(new Error(`missing ${SECRET}`), { code: grpc.status.NOT_FOUND })),
      Slow: (_call, cb) => setTimeout(() => cb(null, Buffer.from([1])), 300),
    },
  );
  const port = await new Promise((resolve, reject) => server.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (err, p) => (err ? reject(err) : resolve(p))));
  const client = new grpc.Client(`127.0.0.1:${port}`, grpc.credentials.createInsecure());
  const unary = (name) => new Promise((resolve) => client.makeUnaryRequest(P + name, identity, identity, Buffer.alloc(0), (err) => resolve(err?.code ?? 0)));
  let out;
  try {
    out = await records(async () => {
      assert.equal(await unary('Internal'), grpc.status.INTERNAL);
      assert.equal(await unary('NotFound'), grpc.status.NOT_FOUND);
      const cancelled = new Promise((resolve) => {
        const call = client.makeUnaryRequest(`${P}Slow`, identity, identity, Buffer.alloc(0), (err) => resolve(err?.code ?? 0));
        setTimeout(() => call.cancel(), 30);
      });
      assert.equal(await cancelled, grpc.status.CANCELLED);
      await until(() => exporter.getFinishedSpans().filter((s) => s.name.startsWith('a37.v1.Svc/')).length >= 6);
    });
  } finally {
    client.close();
    server.forceShutdown();
  }
  const ours = exporter.getFinishedSpans().filter((s) => s.name.startsWith('a37.v1.Svc/'));
  assert.equal(ours.length, 6, ours.map((s) => `${s.name}/${s.kind}`).join());
  for (const s of ours) assertNoDetail(s);
  const internalServer = ours.find((s) => s.name === 'a37.v1.Svc/Internal' && s.kind === SERVER);
  const notFoundServer = ours.find((s) => s.name === 'a37.v1.Svc/NotFound' && s.kind === SERVER);
  assert.equal(internalServer.status.code, ERROR);
  assert.equal(notFoundServer.status.code, UNSET);
  const bySpan = Object.fromEntries(out.map((r) => [r.span_id, r]));
  assert.equal(bySpan[internalServer.spanContext().spanId]?.msg, `internal ${SECRET}`);
  assert.equal(bySpan[internalServer.spanContext().spanId]?.['error.type'], 'INTERNAL');
  assert.equal(bySpan[notFoundServer.spanContext().spanId], undefined, 'R1: NOT_FOUND is not a server error');
  for (const s of ours.filter((x) => x.kind === CLIENT)) assert.equal(bySpan[s.spanContext().spanId], undefined, `client ${s.name}`);
  assert.equal(out.length, 1, JSON.stringify(out.map((r) => r.msg)));
});
