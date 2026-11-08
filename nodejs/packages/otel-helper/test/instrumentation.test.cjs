'use strict';
// Core instrumentations (task B6): contract R7, R10, D5; Node design D-JS1,
// D-JS7, D-JS8. The parity fixture covers the resolved behavior (http.*,
// grpc.*, metrics.http_server_duration_histogram_name); these tests pin what
// the health sampler relies on (url.path at span start, the gRPC span-name
// format), the client exclusions (health paths, the helper's OTLP/HTTP
// endpoints), the helper's /metrics listener staying out of the HTTP server
// metrics, the binding to the providers, and the lifecycle (disabled by
// shutdown, enabled and re-bound by the next setup).
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const api = require('@opentelemetry/api');
const { InMemorySpanExporter, SimpleSpanProcessor, TracerProvider } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const instr = require('../dist/internal/instrumentation.js');
const tracing = require('../dist/internal/tracing.js');
const path = require('node:path');
const { runCjs, runEsm, runNode, lastJson } = require('./helpers.cjs');

const SERVER = api.SpanKind.SERVER;
const CLIENT = api.SpanKind.CLIENT;

let records;
let saved;

function resetEnv() {
  for (const k of Object.keys(process.env)) {
    if (k.startsWith('OTEL_') || k === 'SERVICE_NAME' || k === 'ENVIRONMENT') delete process.env[k];
  }
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
}

test.beforeEach(() => {
  saved = { ...process.env };
  resetEnv();
  testing.reset();
  records = [];
  testing.setDiagnosticSink((line, meta) => records.push({ line, ...meta }));
});

test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

/** A span recorder for setup({ spanProcessors }) that also keeps the attributes each span had at start. */
function recorder() {
  const exporter = new InMemorySpanExporter();
  const atStart = new Map();
  const startSpy = {
    onStart(span) {
      atStart.set(span, { ...span.attributes });
    },
    onEnd() {},
    forceFlush: () => Promise.resolve(),
    shutdown: () => Promise.resolve(),
  };
  return { exporter, atStart, spanProcessors: [startSpy, new SimpleSpanProcessor({ exporter })] };
}

// Only picks a candidate port: setupWithListener() waits for `listening` and
// retries on a bind error, so a port taken meanwhile is not a flaky failure.
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/**
 * setup() with the helper /metrics listener on a free port. The port is free
 * when picked but another process may bind it first: wait for the listener's
 * `listening`, and on a bind error shut down and try another port.
 */
async function setupWithListener(options) {
  for (let attempt = 0; attempt < 5; attempt++) {
    const port = await freePort();
    process.env.OTEL_HELPER_METRICS_PORT = String(port);
    helper.setup(options);
    const listener = testing.getState().runtime.active.listener;
    const ok = await new Promise((resolve) => {
      if (listener.listening) return resolve(true);
      listener.once('listening', () => resolve(true));
      listener.once('error', () => resolve(false));
    });
    if (ok) return port;
    await helper.shutdown();
    testing.reset();
    testing.setDiagnosticSink((line, meta) => records.push({ line, ...meta }));
  }
  throw new Error('no free port for the /metrics listener after 5 attempts');
}

// Raw HTTP/1.1 over node:net: never instrumented, so it neither injects a
// traceparent nor counts as an HTTP client request.
function rawGet(port, target) {
  return new Promise((resolve, reject) => {
    const sock = net.connect(port, '127.0.0.1');
    let data = '';
    sock.setEncoding('utf8');
    sock.on('connect', () => sock.write(`GET ${target} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n`));
    sock.on('data', (d) => {
      data += d;
    });
    sock.on('end', () => resolve(data));
    sock.on('error', reject);
    sock.setTimeout(10_000, () => sock.destroy(new Error(`GET ${target} timed out`)));
  });
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve(server.address().port));
  });
}

function close(server) {
  return new Promise((resolve) => server.close(resolve));
}

/** Sum of the samples whose line matches `nameRegex`. */
function countIn(text, nameRegex) {
  let total = 0;
  for (const line of text.split('\n')) {
    if (line.startsWith('#') || !nameRegex.test(line)) continue;
    total += Number(line.trim().split(/\s+/).pop());
  }
  return total;
}

const SERVER_COUNT = /^http_server_request_duration_seconds_count[{ ]/;
const CLIENT_COUNT = /^http_client_request_duration_seconds_count[{ ]/;

/** An instrumented node:http app server (required through the patched module). */
async function appServer() {
  const server = http.createServer((req, res) => {
    req.resume();
    req.on('end', () => res.end('ok'));
  });
  const port = await listen(server);
  return { server, port };
}

function clientGet(url, options = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { agent: false, ...options }, (res) => {
      res.resume();
      res.on('end', resolve);
    });
    req.on('error', reject);
    req.end();
  });
}

// ── hooks (unit) ────────────────────────────────────────────────────────

test('ignoreOutgoing: health paths by the D5 rule (exact, case-insensitive, trailing slash and query ignored)', () => {
  const ex = instr.clientExclusions(tracing.DEFAULT_HEALTH_PATHS, []);
  for (const path of ['/health', '/Healthz/', '/ready?probe=1', '/metrics#x', '/ping']) {
    assert.equal(instr.ignoreOutgoing({ hostname: 'h', path }, ex), true, path);
  }
  for (const path of ['/healthcare', '/users/ping', '/health/live', '/orders']) {
    assert.equal(instr.ignoreOutgoing({ hostname: 'h', path }, ex), false, path);
  }
  assert.equal(instr.ignoreOutgoing({ hostname: 'h' }, ex), false, 'no path = /');
  assert.equal(instr.ignoreOutgoing({ hostname: 'h', path: '/health' }, instr.clientExclusions([], [])), false, '[] = no health path');
  assert.equal(instr.ignoreOutgoing({ hostname: 'h', path: '/Status/' }, instr.clientExclusions(['/status'], [])), true);
});

test('ignoreOutgoing / ignoreUndici: the helper OTLP/HTTP endpoints (scheme, host, port, path; query ignored)', () => {
  const plan = {
    traces: { protocol: 'http/protobuf', url: 'http://Collector.example.com:4318/v1/traces' },
    metrics: { protocol: 'http/protobuf', url: 'https://[::1]/otlp/v1/metrics' },
    logs: { protocol: 'grpc', url: 'http://collector.example.com:4317' },
  };
  const targets = instr.otlpTargets(plan, { traces: true, metrics: true, logs: true });
  assert.deepEqual(targets, [
    { protocol: 'http:', hostname: 'collector.example.com', port: '4318', pathname: '/v1/traces' },
    { protocol: 'https:', hostname: '::1', port: '443', pathname: '/otlp/v1/metrics' },
  ], 'gRPC endpoints are not HTTP requests');
  assert.deepEqual(instr.otlpTargets(plan, { traces: false, metrics: false, logs: true }), [], 'only signals exported over OTLP');
  assert.deepEqual(instr.otlpTargets({ traces: { protocol: 'http/protobuf', url: 'not a url' } }, { traces: true }), []);
  const ex = instr.clientExclusions([], targets);
  const out = (o) => instr.ignoreOutgoing(o, ex);
  assert.equal(out({ protocol: 'http:', hostname: 'collector.example.com', port: 4318, path: '/v1/traces' }), true);
  assert.equal(out({ protocol: 'http:', hostname: 'COLLECTOR.example.com', port: '4318', path: '/v1/traces?x=1' }), true);
  assert.equal(out({ host: 'collector.example.com', port: 4318, path: '/v1/traces' }), true, 'no protocol, host only');
  assert.equal(out({ protocol: 'https:', hostname: '::1', path: '/otlp/v1/metrics' }), true, 'scheme default port');
  assert.equal(out({ protocol: 'https:', hostname: '[::1]', port: 443, path: '/otlp/v1/metrics' }), true);
  assert.equal(out({ protocol: 'https:', hostname: 'collector.example.com', port: 4318, path: '/v1/traces' }), false, 'scheme differs');
  assert.equal(out({ protocol: 'http:', hostname: 'collector.example.com', port: 4319, path: '/v1/traces' }), false, 'port differs');
  assert.equal(out({ protocol: 'http:', hostname: 'other.example.com', port: 4318, path: '/v1/traces' }), false, 'host differs');
  assert.equal(out({ protocol: 'http:', hostname: 'collector.example.com', port: 4318, path: '/v1/logs' }), false, 'path differs');
  const und = (origin, path) => instr.ignoreUndici({ origin, path }, ex);
  assert.equal(und('http://collector.example.com:4318', '/v1/traces'), true);
  assert.equal(und('https://[::1]', '/otlp/v1/metrics?a'), true);
  assert.equal(und('http://collector.example.com:4318', '/orders'), false);
  assert.equal(und('not an origin', '/v1/traces'), false, 'never throws');
  assert.equal(instr.ignoreUndici({ origin: 'http://h', path: '/HEALTH' }, instr.clientExclusions(tracing.DEFAULT_HEALTH_PATHS, [])), true);
  const throwing = { get path() { throw new Error('boom'); } };
  assert.equal(instr.ignoreOutgoing(throwing, ex), false, 'never throws');
  assert.equal(instr.ignoreUndici(throwing, ex), false, 'never throws');
});

test('ignoreOutgoing: an absolute-form target (proxied request) is matched by its URL pathname', () => {
  const ex = instr.clientExclusions(tracing.DEFAULT_HEALTH_PATHS, [
    { protocol: 'http:', hostname: 'proxy.example.com', port: '3128', pathname: '/v1/traces' },
  ]);
  assert.equal(instr.ignoreOutgoing({ hostname: 'proxy.example.com', port: 3128, path: 'http://target.example.com/healthz?x=1' }, ex), true);
  assert.equal(instr.ignoreOutgoing({ hostname: 'proxy.example.com', port: 3128, path: 'http://target.example.com/orders' }, ex), false);
  assert.equal(instr.ignoreOutgoing({ protocol: 'http:', hostname: 'proxy.example.com', port: 3128, path: 'http://c.example.com/v1/traces' }, ex), true);
  assert.equal(instr.ignoreOutgoing({ hostname: 'h', path: 'not-a-url' }, ex), false, 'kept as written');
});

test('A22 percent-encoded health paths are decoded before matching (as Go req.URL.Path)', () => {
  assert.equal(tracing.normalizeHealthPath('/%68ealth'), '/health');
  assert.equal(tracing.normalizeHealthPath('/HEALTH%2f'), '/health');
  assert.equal(tracing.normalizeHealthPath('/health%3Fx=1'), '/health', 'a decoded ? cuts as in Go');
  assert.equal(tracing.normalizeHealthPath('/%zz'), '/%zz', 'malformed escape kept');
  assert.equal(tracing.normalizeHealthPath('/%E0%A4%A'), '/%e0%a4%a', 'malformed UTF-8 kept (lowercased)');
  const ex = instr.clientExclusions(tracing.DEFAULT_HEALTH_PATHS, []);
  assert.equal(instr.ignoreOutgoing({ hostname: 'h', path: '/%68ealthz' }, ex), true, 'client side too');
});

test('A22 undici URL attributes: userinfo and signature query values redacted as instrumentation-http does; no url.query on client spans', () => {
  assert.deepEqual(instr.undiciUrlAttributes({ origin: 'http://user:pass@127.0.0.1:8080', path: '/x?sig=s&a=1&X-Amz-Signature=q' }), {
    'url.full': 'http://REDACTED:REDACTED@127.0.0.1:8080/x?sig=REDACTED&a=1&X-Amz-Signature=REDACTED',
    'url.query': undefined,
  });
  assert.deepEqual(instr.undiciUrlAttributes({ origin: 'https://example.com', path: '/x' }), {
    'url.full': 'https://example.com/x',
    'url.query': undefined,
  });
  assert.deepEqual(instr.undiciUrlAttributes({ origin: 'http://h', path: '/x?a%20b&flag' }), {
    'url.full': 'http://h/x?a%20b&flag',
    'url.query': undefined,
  }, 'kept as written without a redacted name (no flag= or + rewrite)');
  assert.deepEqual(instr.undiciUrlAttributes({ origin: 'not an origin', path: '/x' }), {}, 'never throws');
});

test('A22 gRPC server status: only the six semconv codes stay ERROR; client spans and other systems untouched', () => {
  const span = (kind, code, attrs, status = api.SpanStatusCode.ERROR) => ({
    kind,
    status: { code: status, message: 'm' },
    attributes: { 'rpc.system': 'grpc', 'rpc.grpc.status_code': code, ...attrs },
  });
  const p = new tracing.GrpcServerStatusProcessor();
  const run = (s) => {
    p.onStart();
    p.onEnding(s);
    p.onEnd();
    return s.status.code;
  };
  for (const code of [1, 3, 5, 6, 7, 8, 9, 10, 11, 16]) assert.equal(run(span(SERVER, code)), api.SpanStatusCode.UNSET, `code ${code}`);
  for (const code of [2, 4, 12, 13, 14, 15]) assert.equal(run(span(SERVER, code)), api.SpanStatusCode.ERROR, `code ${code}`);
  assert.equal(run(span(CLIENT, 5)), api.SpanStatusCode.ERROR, 'client spans keep the upstream rule');
  assert.equal(run(span(SERVER, 5, { 'rpc.system': 'connect_rpc' })), api.SpanStatusCode.ERROR);
  assert.equal(run(span(SERVER, '5')), api.SpanStatusCode.ERROR, 'a non-numeric code is left alone');
  assert.equal(run(span(SERVER, 5, {}, api.SpanStatusCode.OK)), api.SpanStatusCode.OK);
  const throwing = { kind: SERVER, get status() { throw new Error('boom'); } };
  assert.doesNotThrow(() => p.onEnding(throwing), 'never throws on the RPC path');
  return Promise.all([p.forceFlush(), p.shutdown()]);
});

test('A22 gRPC server status through a real TracerProvider: the exporter and a later processor both see UNSET', async () => {
  const exporter = new InMemorySpanExporter();
  const seenOnEnd = [];
  const later = { onStart() {}, onEnd: (s) => seenOnEnd.push(s.status.code), forceFlush: () => Promise.resolve(), shutdown: () => Promise.resolve() };
  const tp = new TracerProvider({ spanProcessors: [new tracing.GrpcServerStatusProcessor(), later, new SimpleSpanProcessor({ exporter })] });
  const tracer = tp.getTracer('t');
  for (const code of [5, 13]) {
    const span = tracer.startSpan('grpc.parity.v1.Echo/X', { kind: SERVER, attributes: { 'rpc.system': 'grpc' } });
    span.setAttribute('rpc.grpc.status_code', code);
    span.setStatus({ code: api.SpanStatusCode.ERROR, message: 'm' });
    span.end();
  }
  // If an SDK release makes Span.status read-only in onEnding, this fails (B13 checklist).
  assert.deepEqual(exporter.getFinishedSpans().map((s) => s.status.code), [api.SpanStatusCode.UNSET, api.SpanStatusCode.ERROR]);
  assert.deepEqual(seenOnEnd, [api.SpanStatusCode.UNSET, api.SpanStatusCode.ERROR]);
  await tp.shutdown();
});

test('ignoreIncoming: only the helper /metrics listener; health requests are not ignored (the sampler drops their spans)', () => {
  assert.equal(instr.ignoreIncoming({ socket: { server: http.createServer() } }), false);
  assert.equal(instr.ignoreIncoming({ socket: null }), false);
  assert.equal(instr.ignoreIncoming({}), false);
  assert.equal(instr.ignoreIncoming({ get socket() { throw new Error('boom'); } }), false, 'never throws');
});

// ── wiring (in process) ────────────────────────────────────────────────

test('D-JS7: an HTTP server span carries url.path at start (what the health sampler reads); R10 metric attributes', async () => {
  process.env.OTEL_METRICS_EXPORTER = 'prometheus';
  const rec = recorder();
  helper.setup({ spanProcessors: rec.spanProcessors });
  const express = require('express');
  const app = express();
  app.use((_req, _res, next) => next());
  app.get('/orders/:id', (_req, res) => res.send('ok'));
  const server = http.createServer(app);
  const port = await listen(server);
  try {
    await rawGet(port, '/orders/7?x=1');
  } finally {
    await close(server);
  }
  const spans = rec.exporter.getFinishedSpans();
  const serverSpan = spans.find((s) => s.kind === SERVER);
  assert.ok(serverSpan, JSON.stringify(spans.map((s) => s.name)));
  assert.equal(rec.atStart.get(serverSpan)['url.path'], '/orders/7', 'url.path at start, query cut');
  assert.equal(serverSpan.name, 'GET /orders/:id', 'named from the express route');
  const names = spans.map((s) => s.name);
  assert.ok(names.includes('request handler - /orders/:id'), JSON.stringify(names));
  assert.ok(!names.some((n) => n.startsWith('middleware')), `middleware layers ignored: ${JSON.stringify(names)}`);
  assert.ok(!names.some((n) => n.startsWith('router')), `router layers ignored (A22): ${JSON.stringify(names)}`);
  const { text } = await scrapeMounted();
  const line = text.split('\n').find((l) => SERVER_COUNT.test(l));
  assert.ok(line, text);
  for (const label of ['http_request_method="GET"', 'http_response_status_code="200"', 'http_route="/orders/:id"', 'url_scheme="http"']) {
    assert.ok(line.includes(label), `${label} in ${line}`);
  }
});

/** Calls the mounted handler on an uninstrumented in-memory response. */
function scrapeMounted() {
  const handler = helper.metricsHandler();
  return new Promise((resolve) => {
    const chunks = [];
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
      write(chunk) {
        chunks.push(String(chunk));
        return true;
      },
      end(chunk) {
        if (chunk !== undefined && typeof chunk !== 'function') chunks.push(String(chunk));
        resolve({ status: this.statusCode, text: chunks.join('') });
        return this;
      },
    };
    handler({ method: 'GET', url: '/metrics', headers: {} }, res);
  });
}

test('D-JS7: gRPC spans are named grpc.<service>/<method> at start; health CLIENT and SERVER spans dropped by that name', async () => {
  const seen = [];
  const original = tracing.HealthCheckSampler.prototype.shouldSample;
  tracing.HealthCheckSampler.prototype.shouldSample = function spy(ctx, traceId, name, kind, ...rest) {
    seen.push({ name, kind });
    return original.call(this, ctx, traceId, name, kind, ...rest);
  };
  try {
    const rec = recorder();
    helper.setup({ spanProcessors: rec.spanProcessors });
    const grpc = require('@grpc/grpc-js');
    const identity = (b) => b;
    const def = (path) => ({ path, requestStream: false, responseStream: false, requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity });
    const server = new grpc.Server();
    const unary = (_call, cb) => cb(null, Buffer.from([0x08, 0x01]));
    server.addService({ Check: def('/grpc.health.v1.Health/Check') }, { Check: unary });
    server.addService({ Ping: def('/parity.v1.Echo/Ping') }, { Ping: unary });
    const port = await new Promise((resolve, reject) => {
      server.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (err, p) => (err ? reject(err) : resolve(p)));
    });
    const client = new grpc.Client(`127.0.0.1:${port}`, grpc.credentials.createInsecure());
    const call = (path) =>
      new Promise((resolve, reject) => client.makeUnaryRequest(path, identity, identity, Buffer.alloc(0), (err) => (err ? reject(err) : resolve())));
    try {
      await call('/grpc.health.v1.Health/Check');
      await call('/parity.v1.Echo/Ping');
    } finally {
      client.close();
      await new Promise((resolve) => server.tryShutdown(() => resolve()));
    }
    // The sampler sees the semconv name (A30): the helper starts instrumentation-grpc's spans under it.
    const grpcSeen = seen.filter((s) => s.name.endsWith('/Check') || s.name.endsWith('/Ping'));
    assert.deepEqual(grpcSeen, [
      { name: 'grpc.health.v1.Health/Check', kind: CLIENT },
      { name: 'grpc.health.v1.Health/Check', kind: SERVER },
      { name: 'parity.v1.Echo/Ping', kind: CLIENT },
      { name: 'parity.v1.Echo/Ping', kind: SERVER },
    ]);
    const spans = rec.exporter.getFinishedSpans().map((s) => ({ name: s.name, kind: s.kind }));
    assert.deepEqual(
      spans.sort((a, b) => a.kind - b.kind),
      [
        { name: 'parity.v1.Echo/Ping', kind: SERVER },
        { name: 'parity.v1.Echo/Ping', kind: CLIENT },
      ],
      'health Check traced on neither side',
    );
  } finally {
    tracing.HealthCheckSampler.prototype.shouldSample = original;
  }
});

test('D-JS8: two scrapes of the helper /metrics listener leave the HTTP server metrics unchanged (no span either)', async () => {
  const rec = recorder();
  const metricsPort = await setupWithListener({ spanProcessors: rec.spanProcessors });
  const { server, port } = await appServer();
  try {
    await rawGet(port, '/orders');
  } finally {
    await close(server);
  }
  const first = await rawGet(metricsPort, '/metrics');
  const second = await rawGet(metricsPort, '/metrics');
  assert.equal(countIn(first, SERVER_COUNT), 1, first);
  assert.equal(countIn(second, SERVER_COUNT), 1, 'the first scrape was not counted');
  assert.equal(rec.exporter.getFinishedSpans().filter((s) => s.kind === SERVER).length, 1, 'scrapes are not traced');
});

test('R7 HTTP clients: a health path gets no span and no client metric (node:http and fetch); other paths both', async () => {
  process.env.OTEL_METRICS_EXPORTER = 'prometheus';
  const rec = recorder();
  helper.setup({ spanProcessors: rec.spanProcessors });
  const { server, port } = await appServer();
  try {
    await clientGet(`http://127.0.0.1:${port}/healthz?probe=1`);
    await clientGet(`http://127.0.0.1:${port}/orders`);
    await (await fetch(`http://127.0.0.1:${port}/Ready/`)).text();
    await (await fetch(`http://127.0.0.1:${port}/orders`)).text();
  } finally {
    await close(server);
  }
  const clients = rec.exporter.getFinishedSpans().filter((s) => s.kind === CLIENT);
  assert.equal(clients.length, 2, JSON.stringify(clients.map((s) => s.instrumentationScope.name)));
  assert.deepEqual(
    clients.map((s) => s.instrumentationScope.name).sort(),
    ['@opentelemetry/instrumentation-http', '@opentelemetry/instrumentation-undici'],
  );
  const { text } = await scrapeMounted();
  assert.equal(countIn(text, CLIENT_COUNT), 2, text);
  assert.equal(countIn(text, SERVER_COUNT), 4, 'every request is counted on the server, health included');
});

test('D-JS7: requests to the helper OTLP/HTTP endpoint are not counted as HTTP client requests', async () => {
  const posts = [];
  const sink = http.createServer((req, res) => {
    posts.push(`${req.method} ${req.url}`);
    req.resume();
    req.on('end', () => res.end());
  });
  const port = await listen(sink);
  try {
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = `http://127.0.0.1:${port}`;
    process.env.OTEL_EXPORTER_OTLP_PROTOCOL = 'http/protobuf';
    process.env.OTEL_METRICS_EXPORTER = 'otlp,prometheus';
    helper.setup();
    api.trace.getTracer('t').startSpan('exported').end();
    await testing.getProviders().tracerProvider.forceFlush();
    assert.ok(posts.includes('POST /v1/traces'), JSON.stringify(posts));
    // The same request made by the app through the patched module is filtered too.
    await clientGet(new URL(`http://127.0.0.1:${port}/v1/traces`), { method: 'POST' });
    await clientGet(`http://127.0.0.1:${port}/other`);
    const { text } = await scrapeMounted();
    assert.equal(countIn(text, CLIENT_COUNT), 1, text);
  } finally {
    await close(sink);
  }
});

test('A22 redaction: node:http and fetch clients redact userinfo and signature values; server url.query as recorded', async () => {
  const rec = recorder();
  helper.setup({ spanProcessors: rec.spanProcessors });
  const { server, port } = await appServer();
  try {
    await clientGet(`http://user:pass@127.0.0.1:${port}/x?sig=s&a=1`);
    await (await fetch(`http://127.0.0.1:${port}/y?sig=s&a=1`)).text();
    await rawGet(port, '/z?token=s&sig=t');
  } finally {
    await close(server);
  }
  const spans = rec.exporter.getFinishedSpans();
  const clients = spans.filter((s) => s.kind === CLIENT);
  assert.equal(clients.length, 2);
  for (const c of clients) {
    const full = String(c.attributes['url.full']);
    assert.ok(!full.includes('user:pass') && !full.includes('sig=s'), full);
    assert.ok(full.includes('sig=REDACTED'), full);
  }
  const undici = clients.find((c) => c.instrumentationScope.name === '@opentelemetry/instrumentation-undici');
  const httpClient = clients.find((c) => c.instrumentationScope.name === '@opentelemetry/instrumentation-http');
  assert.equal('url.query' in undici.attributes, false, 'no url.query on client spans (as node:http and Go)');
  assert.equal('url.query' in httpClient.attributes, false);
  assert.match(String(httpClient.attributes['url.full']), /^http:\/\/REDACTED:REDACTED@127\.0\.0\.1:\d+\/x\?sig=REDACTED&a=1$/);
  // Server side (instrumentation-http, recorded in D-JS14): only the signature list is redacted; `token` is kept.
  const z = spans.find((s) => s.kind === SERVER && s.attributes['url.path'] === '/z');
  assert.equal(z.attributes['url.query'], 'token=s&sig=REDACTED');
  assert.ok(!records.some((r) => r.level === 'warn' || r.level === 'error'), JSON.stringify(records));
});

test('A30 redaction: access_token, X-Goog-Credential and GoogleAccessId on node:http, fetch and server url.query', async () => {
  assert.deepEqual(instr.REDACTED_QUERY_PARAMS.slice(7), ['access_token', 'X-Goog-Credential', 'GoogleAccessId']);
  const rec = recorder();
  helper.setup({ spanProcessors: rec.spanProcessors });
  const { server, port } = await appServer();
  const q = 'access_token=AT1&X-Goog-Credential=GC1&GoogleAccessId=GA1&Access_Token=keep&a=1';
  try {
    await clientGet(`http://127.0.0.1:${port}/x?${q}`);
    await (await fetch(`http://127.0.0.1:${port}/y?${q}`)).text();
    await rawGet(port, `/z?${q}`);
  } finally {
    await close(server);
  }
  const spans = rec.exporter.getFinishedSpans();
  const texts = [
    ...spans.filter((s) => s.kind === CLIENT).map((s) => String(s.attributes['url.full'])),
    ...spans.filter((s) => s.kind === SERVER && s.attributes['url.query'] !== undefined).map((s) => String(s.attributes['url.query'])),
  ];
  assert.ok(texts.length >= 4, JSON.stringify(texts));
  for (const t of texts) {
    for (const secret of ['AT1', 'GC1', 'GA1']) assert.ok(!t.includes(secret), t);
    assert.ok(t.includes('access_token=REDACTED') && t.includes('X-Goog-Credential=REDACTED') && t.includes('GoogleAccessId=REDACTED'), t);
    assert.ok(t.includes('Access_Token=keep'), `case-sensitive names: ${t}`);
  }
});

test('lifecycle: shutdown disables the instrumentations; the next setup enables them and binds its own providers', async () => {
  const express = require('express');
  const grpc = require('@grpc/grpc-js');
  const dc = require('node:diagnostics_channel');
  const patched = () => ({
    http: http.request.__wrapped === true,
    express: express.application.use.__wrapped === true,
    grpc: grpc.Client.prototype.makeUnaryRequest.__wrapped === true && grpc.Server.prototype.register.__wrapped === true,
    undici: dc.hasSubscribers('undici:request:create'),
  });
  const all = (value) => ({ http: value, express: value, grpc: value, undici: value });
  const a = recorder();
  helper.setup({ spanProcessors: a.spanProcessors });
  assert.deepEqual(patched(), all(true), 'patched while active');
  await helper.shutdown();
  assert.deepEqual(patched(), all(false), 'unpatched by shutdown');
  const b = recorder();
  helper.setup({ spanProcessors: b.spanProcessors });
  assert.deepEqual(patched(), all(true), 'patched again');
  const { server, port } = await appServer();
  try {
    await rawGet(port, '/orders');
    await (await fetch(`http://127.0.0.1:${port}/orders`)).text();
  } finally {
    await close(server);
  }
  assert.equal(a.exporter.getFinishedSpans().length, 0);
  const kinds = b.exporter.getFinishedSpans().map((s) => s.kind);
  assert.equal(kinds.filter((k) => k === SERVER).length, 2, 're-bound to the new TracerProvider');
  assert.equal(kinds.filter((k) => k === CLIENT).length, 1, 'fetch re-bound too');
  testing.reset();
  assert.deepEqual(patched(), all(false), '/testing reset() disables them');
});

test('a foreign global TracerProvider: the instrumentations bind to it, so their spans join the app spans', async () => {
  const exporter = new InMemorySpanExporter();
  const foreign = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  api.trace.setGlobalTracerProvider(foreign);
  try {
    helper.setup();
    const { server, port } = await appServer();
    try {
      await rawGet(port, '/orders');
    } finally {
      await close(server);
    }
    assert.equal(exporter.getFinishedSpans().filter((s) => s.kind === SERVER).length, 1);
  } finally {
    api.trace.disable();
    await foreign.shutdown();
  }
});

test('an instrumentation that fails to enable is reported; setup still returns and shutdown works', async () => {
  helper.setup();
  await helper.shutdown();
  const holder = testing.getState().runtime.instrumentations;
  const first = holder.instrumentations[0];
  const enable = first.enable;
  first.enable = () => {
    throw new Error('boom');
  };
  try {
    testing.reset();
    records = [];
    testing.setDiagnosticSink((line, meta) => records.push({ line, ...meta }));
    helper.setup();
    assert.ok(
      records.some((r) => r.line === 'otelhelper: the instrumentations could not be enabled; http, fetch, express and gRPC are not instrumented error=boom'),
      JSON.stringify(records),
    );
    assert.equal(testing.getState().runtime.active.instrumented, false);
    await helper.shutdown();
  } finally {
    first.enable = enable;
  }
});

// ── separate processes (module patch state) ─────────────────────────────

test('D-JS1: the app grpc-js is patched when only gRPC OTLP export is configured (the exporter loaded it first)', () => {
  const res = runCjs(`
    for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
    process.env.OTEL_SERVICE_NAME = 'svc';
    process.env.OTEL_HELPER_METRICS_PORT = '0';
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://127.0.0.1:1';
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
    const helper = require('@staffops/otel-helper');
    helper.setup();
    const preloadedReports = lines.filter((l) => l.includes('was loaded before setup()'));
    const loadedByExporter = Object.keys(require.cache).some((f) => f.includes('@grpc/grpc-js'));
    const grpc = require('@grpc/grpc-js');
    console.log(JSON.stringify({ loadedByExporter, client: grpc.Client.prototype.makeUnaryRequest.__wrapped === true, server: grpc.Server.prototype.register.__wrapped === true, preloadedReports }));
    process.exit(0);
  `);
  assert.deepEqual(lastJson(res), { loadedByExporter: true, client: true, server: true, preloadedReports: [] });
});

test('express and grpc-js required before setup() are instrumented and reported once each (CJS)', () => {
  const res = runCjs(`
    const express = require('express');
    const grpc = require('@grpc/grpc-js');
    for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
    process.env.OTEL_SERVICE_NAME = 'svc';
    process.env.OTEL_HELPER_METRICS_PORT = '0';
    const lines = [];
    const testing = require('@staffops/otel-helper/testing');
    testing.setDiagnosticSink((l) => lines.push(l));
    const helper = require('@staffops/otel-helper');
    helper.setup();
    const patched = express.application.use.__wrapped === true && grpc.Client.prototype.makeUnaryRequest.__wrapped === true && grpc.Server.prototype.register.__wrapped === true;
    helper.shutdown().then(() => {
      helper.setup();
      const full = lines.filter((l) => l.includes('was loaded before setup()'));
      console.log(JSON.stringify({ patched, again: express.application.use.__wrapped === true, reports: full.map((l) => l.split(' was loaded')[0]), fullReports: full }));
      process.exit(0);
    });
  `);
  const summary = { ...lastJson(res) };
  delete summary.fullReports;
  assert.deepEqual(summary, {
    patched: true,
    again: true,
    reports: ['otelhelper: express', 'otelhelper: @grpc/grpc-js'],
  });
  for (const line of lastJson(res).fullReports) {
    // B9: the register entry calls setup(), so it is the cure the report names.
    assert.match(line, / was loaded before setup\(\); apps, routers and clients created before setup\(\) may lack spans or have an incomplete http\.route — start the process with --import @staffops\/otel-helper\/register \(or a preload that imports @staffops\/otel-helper\/hooks and calls setup\(\)\) so setup\(\) runs before the app loads module=/);
  }
});

test('express loaded early: a route registered after setup() gets a request-handler span and http.route (express 5 router)', () => {
  const res = runCjs(`
    const express = require('express');
    for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
    process.env.OTEL_SERVICE_NAME = 'svc';
    process.env.OTEL_HELPER_METRICS_PORT = '0';
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
    const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
    const exporter = new InMemorySpanExporter();
    require('@staffops/otel-helper').setup({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
    const http = require('node:http');
    const app = express();
    app.get('/orders/:id', (_req, res) => res.send('ok'));
    const server = app.listen(0, '127.0.0.1', () => {
      http.get('http://127.0.0.1:' + server.address().port + '/orders/7', (r) => {
        r.resume();
        r.on('end', () => server.close(() => {
          const spans = exporter.getFinishedSpans();
          const srv = spans.find((s) => s.kind === 1);
          console.log(JSON.stringify({ names: spans.map((s) => s.name).filter((n) => n.startsWith('request handler')), route: srv && srv.attributes['http.route'] }));
          process.exit(0);
        }));
      });
    });
  `);
  assert.deepEqual(lastJson(res), { names: ['request handler - /orders/:id'], route: '/orders/:id' });
});

test('M1: an ESM import with the loader hooks is not reported; without the hooks it is', () => {
  const fixture = path.join(__dirname, 'fixtures', 'esm-early-express.mjs');
  const env = { OTEL_SERVICE_NAME: 'svc', OTEL_HELPER_METRICS_PORT: '0' };
  const withHooks = lastJson(runNode(['--import', '@staffops/otel-helper/hooks', fixture], { env }));
  assert.deepEqual(withHooks, { patched: true, reports: 0 });
  const without = lastJson(runNode([fixture], { env }));
  assert.deepEqual(without, { patched: true, reports: 1 });
});

test('a package loaded only through a deep path before setup() is reported and its main entry re-required', () => {
  const res = runCjs(`
    require('express/lib/express.js');
    for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
    process.env.OTEL_SERVICE_NAME = 'svc';
    process.env.OTEL_HELPER_METRICS_PORT = '0';
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
    require('@staffops/otel-helper').setup();
    const express = require('express');
    console.log(JSON.stringify({ patched: express.application.use.__wrapped === true, reports: lines.filter((l) => l.includes('was loaded before setup()')).length }));
    process.exit(0);
  `);
  assert.deepEqual(lastJson(res), { patched: true, reports: 1 });
});

test('ESM named imports of node:http taken before setup() see the patched functions (syncBuiltinESMExports)', () => {
  const res = runEsm(`
    import { request, get } from 'node:http';
    for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
    process.env.OTEL_SERVICE_NAME = 'svc';
    process.env.OTEL_HELPER_METRICS_PORT = '0';
    const testing = await import('@staffops/otel-helper/testing');
    testing.setDiagnosticSink(() => {});
    const helper = await import('@staffops/otel-helper');
    helper.setup();
    const active = request.__wrapped === true && get.__wrapped === true;
    await helper.shutdown();
    console.log(JSON.stringify({ active, afterShutdown: request.__wrapped === true }));
    process.exit(0);
  `);
  assert.deepEqual(lastJson(res), { active: true, afterShutdown: false });
});

test('OTEL_SDK_DISABLED=true: nothing is instrumented (Python: no instrumentation)', () => {
  const res = runCjs(`
    for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
    process.env.OTEL_SERVICE_NAME = 'svc';
    process.env.OTEL_SDK_DISABLED = 'true';
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
    require('@staffops/otel-helper').setup();
    const http = require('node:http');
    const state = require('@staffops/otel-helper/testing').getState();
    console.log(JSON.stringify({ wrapped: http.request.__wrapped === true, built: state.runtime.instrumentations !== undefined }));
  `);
  assert.deepEqual(lastJson(res), { wrapped: false, built: false });
});

test('node:http loaded before setup() and never required again is patched by setup()', () => {
  const res = runCjs(`
    const http = require('node:http');
    for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
    process.env.OTEL_SERVICE_NAME = 'svc';
    process.env.OTEL_HELPER_METRICS_PORT = '0';
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
    require('@staffops/otel-helper').setup();
    console.log(JSON.stringify({ request: http.request.__wrapped === true, emit: http.Server.prototype.emit.__wrapped === true }));
    process.exit(0);
  `);
  assert.deepEqual(lastJson(res), { request: true, emit: true });
});
