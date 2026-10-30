'use strict';
// Parity fixture driver: runs ONE case of parity/cases.json in a fresh Node
// process (design D-JS12; format: parity/README.md).
//
//   node driver.cjs <case.json> <out.json>
//
// runner.test.cjs applies the case env and expands the {receiver…}
// placeholders before spawning it. OTel globals are set-once per process,
// so every case gets its own process. The snapshot is built only from
// helper-owned state: the frozen `Resolved` that `setup()` returns, the
// providers it installs (public OTel API), the helper's `metricsHandler()`
// and what the fake receiver recorded on the wire. Matching happens in the
// parent.
//
// Where the helper API a kind needs does not exist yet, the driver reports
// `adapter_pending` naming the task that adds it (a hard failure for any
// case that is not skipped, like an adapter error). `adapter_error` is a
// fixture/adapter bug: an unknown kind, scenario or `code` key.
//
// The driver exits with process.exit(): parity children must write their
// NODE_V8_COVERAGE data (merged by c8 into the core package's gate), and
// exporters stuck retrying a wrong port must not keep the process alive.

const fs = require('node:fs');
const path = require('node:path');
const cluster = require('node:cluster');

const SHUTDOWN_DEADLINE_MS = 15_000;
// Export cases may set timeouts above the SDK batch default (e.g. 60 000 ms)
// and must see that export finish (same budget as the Go and Python drivers).
const EXPORT_SHUTDOWN_DEADLINE_MS = 75_000;
const TRACE_ID = '0af7651916cd43dd8448eb211c80319c';
const SPAN_ID = 'b7ad6b7169203331';
const SAMPLED = 1;

// ── diagnostics capture ─────────────────────────────────────────────────
// The sink receives (line, {origin, level}) (design D-JS5).
// helper_log: records with origin `helper` (the helper's own diagnostics, any
// level), never the SDK `diag` messages its DiagLogger forwards.
// diagnostics: every record at warn level or above, whatever its origin, plus
// Node process warnings (emitWarning, e.g. DEP0123 from a dependency) — the
// Python driver's "every WARNING+ record and warnings.warn".
// The capture also writes each record to stderr, so `console` sees what an
// app would.
const captured = [];
const helperRecords = [];
const WARN_OR_ABOVE = new Set(['warn', 'error']);

/** Which snapshot fields a sink record lands in, from its metadata. */
function classify(meta) {
  return { diagnostics: WARN_OR_ABOVE.has(meta?.level), helperLog: meta?.origin === 'helper' };
}

function sinkRecord(line, meta) {
  const where = classify(meta);
  if (where.diagnostics) captured.push(line);
  if (where.helperLog) helperRecords.push(line);
  process.stderr.write(`${line}\n`);
}

process.on('warning', (w) => {
  captured.push(`${w.name}: ${w.code ? `[${w.code}] ` : ''}${w.message}`);
});

class Pending extends Error {}
class AdapterError extends Error {}

let testingApi;
function installSink() {
  // `/testing` exists since B1a; setDiagnosticSink is the hook D-JS5 names.
  testingApi = require('@staffops/otel-helper/testing');
  testingApi.setDiagnosticSink(sinkRecord);
}

function helper() {
  return require('@staffops/otel-helper');
}

/** Throws Pending unless every named export is a function. */
function need(mod, names, task, where = '@staffops/otel-helper') {
  const missing = names.filter((n) => typeof mod[n] !== 'function');
  if (missing.length) throw new Pending(`${where} has no ${missing.map((n) => `${n}()`).join(', ')} yet (task ${task})`);
}

// ── code options (fixture snake_case → helper camelCase, design API table) ──
const CODE_KEYS = {
  service_name: 'serviceName',
  environment: 'environment',
  otel_endpoint: 'otelEndpoint',
  traces_endpoint: 'tracesEndpoint',
  metrics_endpoint: 'metricsEndpoint',
  logs_endpoint: 'logsEndpoint',
  otlp_protocol: 'otlpProtocol',
  insecure: 'insecure',
  compression: 'compression',
  export_timeout_ms: 'exportTimeoutMs',
  metric_exporters: 'metricExporters',
  export_interval_ms: 'exportIntervalMs',
  sample_ratio: 'sampleRatio',
  debug: 'debug',
  resource_attributes: 'resourceAttributes',
  health_paths: 'healthPaths',
};

/** Maps a case's `code` object to setup() options; an unmapped key is an adapter error. */
function mapOptions(code) {
  const options = {};
  const unsupported = [];
  for (const key of Object.keys(code ?? {}).sort()) {
    const name = CODE_KEYS[key];
    if (name === undefined) unsupported.push(key);
    else options[name] = key === 'debug' ? Boolean(code[key]) : code[key];
  }
  if (unsupported.length) throw new AdapterError(`unsupported code key(s) ${unsupported.join(', ')}`);
  return options;
}

// `extra` carries driver-only options that are not fixture `code` keys
// (`spanProcessors`, design D-JS12).
function setupCase(c, snap, extra = {}) {
  const h = helper();
  need(h, ['setup', 'shutdown'], 'B3');
  const options = { ...mapOptions(c.code), ...extra };
  try {
    return h.setup(options);
  } catch (err) {
    snap.error = `${err?.name ?? 'Error'}: ${err?.message ?? String(err)}`;
    return undefined;
  }
}

/** Shuts the helper down under one deadline; stragglers are abandoned (process.exit follows). */
async function shutdown(deadlineMs = SHUTDOWN_DEADLINE_MS) {
  const h = helper();
  if (typeof h.shutdown !== 'function') return;
  let timer;
  const deadline = new Promise((resolve) => {
    timer = setTimeout(resolve, deadlineMs);
  });
  try {
    await Promise.race([Promise.resolve().then(() => h.shutdown(deadlineMs)).catch(() => undefined), deadline]);
  } finally {
    clearTimeout(timer);
  }
}

function api() {
  return require('@opentelemetry/api');
}

function attrsOf(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj ?? {})) out[k] = Array.isArray(v) ? v.join(',') : String(v);
  return out;
}

// The resource of the installed TracerProvider, from `/testing` getResource()
// (the 3.0 provider has no public getter; Python reads provider.resource). No
// probe span: it would reach the wire in export cases.
function resourceSnapshot() {
  need(testingApi, ['getResource'], 'B3', '@staffops/otel-helper/testing');
  const resource = testingApi.getResource();
  return resource === undefined ? undefined : attrsOf(resource.attributes);
}

function resolvedField(resolved, name, task) {
  if (resolved === null || typeof resolved !== 'object' || !(name in resolved)) {
    throw new Pending(`Resolved.${name} is not available yet (task ${task})`);
  }
  return resolved[name];
}

// ── kinds ───────────────────────────────────────────────────────────────

async function kindResolve(c) {
  const snap = {};
  const resolved = setupCase(c, snap);
  if (resolved !== undefined) {
    const resource = resourceSnapshot();
    if (resource !== undefined) snap.resource = resource;
    const expect = c.expect ?? {};
    // Read only the fields the case asserts: each lands with its own task.
    if ('metric_exporters' in expect) snap.metric_exporters = [...resolvedField(resolved, 'metricExporters', 'B4c')];
    if ('metric_export_interval_ms' in expect) {
      snap.metric_export_interval_ms = resolvedField(resolved, 'metricExportIntervalMs', 'B4c');
    }
    if ('log_level' in expect) {
      const level = String(resolvedField(resolved, 'logLevel', 'B3')).toUpperCase();
      snap.log_level = level === 'WARNING' ? 'WARN' : level;
    }
  }
  await shutdown();
  return snap;
}

function admin(method) {
  const base = process.env.FAKEOTLP_ADMIN?.replace(/\/+$/, '');
  if (!base) throw new AdapterError('FAKEOTLP_ADMIN is not set (the runner sets it for export cases)');
  return fetch(`${base}/requests`, { method, signal: AbortSignal.timeout(5_000) }).then(async (res) => {
    if (!res.ok) throw new Error(`fake receiver ${method} /requests: HTTP ${res.status}`);
    return res.text();
  });
}

function signalOf(proto, p) {
  const lower = p.toLowerCase();
  const tokens = proto === 'grpc'
    ? [['.trace.', 'traces'], ['.metrics.', 'metrics'], ['.logs.', 'logs']]
    : [['trace', 'traces'], ['metric', 'metrics'], ['log', 'logs']];
  for (const [token, sig] of tokens) if (lower.includes(token)) return sig;
  return 'other';
}

/** grpc-timeout header ("<digits><H|M|S|m|u|n>") in milliseconds, else undefined. */
function grpcTimeoutMs(value) {
  const units = { H: 3.6e6, M: 6e4, S: 1e3, m: 1, u: 1e-3, n: 1e-6 };
  const m = /^(\d+)([HMSmun])$/.exec(value ?? '');
  return m ? Number(m[1]) * units[m[2]] : undefined;
}

function emitLog(name, body) {
  const logs = api().logs;
  if (logs === undefined || typeof logs.getLogger !== 'function') {
    throw new AdapterError('@opentelemetry/api has no logs API (it is part of api >= 1.10)');
  }
  logs.getLogger(name).emit({ severityNumber: 13, severityText: 'WARN', body });
}

async function kindExport(c) {
  // The admin calls use fetch before setup and after shutdown only: the helper
  // instruments undici, and the snapshot must not contain the driver's traffic.
  await admin('DELETE');
  const snap = {};
  const resolved = setupCase(c, snap);
  if (resolved !== undefined) {
    const { trace, metrics } = api();
    trace.getTracer('parity').startSpan('parity-span').end();
    metrics.getMeter('parity').createCounter('parity.requests').add(1);
    emitLog('parity', 'parity-log');
  }
  await shutdown(EXPORT_SHUTDOWN_DEADLINE_MS);
  // Only records sent to this case's receiver addresses count: other tests in
  // the same network namespace may export to localhost:4317 meanwhile.
  const dest = new Set((process.env.FAKEOTLP_DEST_IPS ?? '').split(',').filter(Boolean));
  const received = { traces: [], metrics: [], logs: [], other: [] };
  for (const rec of JSON.parse(await admin('GET'))) {
    if (!dest.has(rec.addr)) continue;
    const out = {
      proto: rec.proto,
      tls: rec.tls,
      port: Number(rec.port),
      path: rec.path,
      raw_path: rec.raw_path ?? '',
      encoding: rec.encoding ?? '',
      headers: rec.headers ?? {},
    };
    const ms = grpcTimeoutMs(rec.timeout);
    if (ms !== undefined) out.timeout_ms = ms;
    received[signalOf(rec.proto, rec.path)].push(out);
  }
  snap.received = received;
  return snap;
}

function remoteParent(sampled, debugTracestate = false) {
  const { trace, ROOT_CONTEXT, createTraceState } = api();
  return trace.setSpanContext(ROOT_CONTEXT, {
    traceId: TRACE_ID,
    spanId: SPAN_ID,
    traceFlags: sampled ? SAMPLED : 0,
    isRemote: true,
    traceState: debugTracestate ? createTraceState('debug=true') : undefined,
  });
}

async function kindSampler(c) {
  const snap = {};
  setupCase(c, snap);
  const { trace, propagation, ROOT_CONTEXT } = api();
  const tracer = trace.getTracer('parity');
  const scenario = c.input?.scenario;
  let span;
  switch (scenario) {
    case 'root':
      span = tracer.startSpan('target', {}, ROOT_CONTEXT);
      break;
    case 'remote_sampled':
      span = tracer.startSpan('target', {}, remoteParent(true));
      break;
    case 'remote_unsampled':
      span = tracer.startSpan('target', {}, remoteParent(false));
      break;
    case 'remote_sampled_debug_tracestate':
      span = tracer.startSpan('target', {}, remoteParent(true, true));
      break;
    case 'remote_sampled_debug_baggage': {
      const bag = propagation.createBaggage({ debug: { value: 'true' } });
      span = tracer.startSpan('target', {}, propagation.setBaggage(remoteParent(true), bag));
      break;
    }
    case 'local_child_of_root':
    case 'local_child_of_remote_unsampled': {
      const parentCtx = scenario === 'local_child_of_root' ? ROOT_CONTEXT : remoteParent(false);
      const parent = tracer.startSpan('parent', {}, parentCtx);
      span = tracer.startSpan('target', {}, trace.setSpan(parentCtx, parent));
      parent.end();
      break;
    }
    default:
      throw new AdapterError(`unknown scenario ${JSON.stringify(scenario)}`);
  }
  snap.sampled = (span.spanContext().traceFlags & SAMPLED) === SAMPLED;
  snap.attributes = attrsOf(span.attributes);
  span.end();
  await shutdown();
  return snap;
}

async function kindPropagation(c) {
  const snap = {};
  setupCase(c, snap);
  const { trace, context, propagation } = api();
  const carrier = {};
  const ctx = propagation.setBaggage(context.active(), propagation.createBaggage({ parity: { value: '1' } }));
  context.with(ctx, () => {
    trace.getTracer('parity').startActiveSpan('parity-span', (span) => {
      propagation.inject(context.active(), carrier);
      span.end();
    });
  });
  snap.headers = Object.keys(carrier).map((k) => k.toLowerCase()).sort();
  await shutdown();
  return snap;
}

// In-memory span capture. The 3.0 SDK accepts span processors only at
// construction, so the driver passes one through the `spanProcessors` setup
// option (test/advanced, not a fixture `code` key; design D-JS7, D-JS12).
function spanRecorder() {
  const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
  const exporter = new InMemorySpanExporter();
  // 3.0 processors take options (spike B1c F7).
  return { exporter, options: { spanProcessors: [new SimpleSpanProcessor({ exporter })] } };
}

const SPAN_KIND_SERVER = 1;
const serverSpans = (exporter) => exporter.getFinishedSpans().filter((s) => s.kind === SPAN_KIND_SERVER).length;

// Calls the helper's mounted handler with in-memory req/res objects: no server
// and no socket, so instrumentation never sees (or counts) the scrape. The mock
// implements the response surface design D-JS8 allows the handler to use.
function scrape() {
  const h = helper();
  need(h, ['metricsHandler'], 'B7');
  const handler = h.metricsHandler();
  return new Promise((resolve, reject) => {
    const chunks = [];
    const headers = {};
    let timer;
    // write/end accept (chunk[, encoding][, callback]) like http.ServerResponse.
    const push = (chunk, enc) => {
      if (chunk !== undefined && chunk !== null && typeof chunk !== 'function') {
        chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), typeof enc === 'string' ? enc : 'utf8'));
      }
    };
    const callbackOf = (...args) => args.find((a) => typeof a === 'function');
    const res = {
      statusCode: 200,
      headersSent: false,
      setHeader: (k, v) => {
        headers[k.toLowerCase()] = v;
      },
      getHeader: (k) => headers[k.toLowerCase()],
      removeHeader: (k) => {
        delete headers[k.toLowerCase()];
      },
      writeHead(status, maybeHeaders) {
        this.statusCode = status;
        if (maybeHeaders && typeof maybeHeaders === 'object') for (const [k, v] of Object.entries(maybeHeaders)) this.setHeader(k, v);
        this.headersSent = true;
        return this;
      },
      write(chunk, enc, cb) {
        push(chunk, enc);
        this.headersSent = true;
        const done = callbackOf(enc, cb);
        if (done) process.nextTick(done);
        return true;
      },
      end(chunk, enc, cb) {
        push(chunk, enc);
        this.headersSent = true;
        clearTimeout(timer);
        resolve({ status: this.statusCode, text: Buffer.concat(chunks).toString('utf8') });
        const done = callbackOf(chunk, enc, cb);
        if (done) process.nextTick(done);
        return this;
      },
      on: () => res,
      once: () => res,
    };
    const req = { method: 'GET', url: '/metrics', headers: { host: 'parity' }, on: () => req, once: () => req };
    timer = setTimeout(() => reject(new Error('metricsHandler did not end the response within 10 s')), 10_000);
    try {
      handler(req, res);
    } catch (err) {
      clearTimeout(timer);
      reject(err);
    }
  });
}

async function scrapeCount(nameRegex) {
  const { text } = await scrape();
  let total = 0;
  for (const line of text.split('\n')) {
    if (line.startsWith('#') || !nameRegex.test(line)) continue;
    const value = Number(line.trim().split(/\s+/).pop());
    if (!Number.isNaN(value)) total += value;
  }
  return total;
}

// Raw HTTP/1.1 over node:net (not instrumented): an instrumented client would
// inject its own traceparent and turn every request into a remote-parent one.
function rawGet(port, target, headers) {
  const net = require('node:net');
  return new Promise((resolve, reject) => {
    const sock = net.connect(port, '127.0.0.1');
    const lines = [`GET ${target} HTTP/1.1`, 'Host: 127.0.0.1', 'Connection: close'];
    for (const [k, v] of Object.entries(headers)) lines.push(`${k}: ${v}`);
    let data = '';
    sock.setEncoding('utf8');
    sock.on('connect', () => sock.write(`${lines.join('\r\n')}\r\n\r\n`));
    sock.on('data', (d) => {
      data += d;
    });
    sock.on('end', () => resolve(data));
    sock.on('error', reject);
    sock.setTimeout(10_000, () => sock.destroy(new Error(`GET ${target} timed out`)));
  });
}

async function kindHttpServer(c) {
  const snap = {};
  need(helper(), ['metricsHandler'], 'B7');
  const { exporter, options } = spanRecorder();
  setupCase(c, snap, options);
  if ('error' in snap) return snap;
  // Required after setup on purpose: the instrumentations patch them on load.
  require('node:http');
  const express = require('express');
  const { trace } = api();
  const childSpan = c.input?.child_span === true;
  const app = express();
  app.use((_req, res) => {
    if (childSpan) trace.getTracer('parity').startSpan('handler-child').end();
    res.send('ok');
  });
  const server = await new Promise((resolve, reject) => {
    const srv = app.listen(0, '127.0.0.1', (err) => (err ? reject(err) : resolve(srv)));
  });
  const { port } = server.address();
  const headers = c.input?.traceparent ? { traceparent: c.input.traceparent } : {};
  try {
    for (const target of c.input?.requests ?? []) await rawGet(port, target, headers);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
  snap.server_spans = serverSpans(exporter);
  snap.handler_child_spans = exporter.getFinishedSpans().filter((s) => s.name === 'handler-child').length;
  snap.requests_counted = await scrapeCount(/^http_server(_request)?_duration[a-z_]*_count[{ ]/);
  await shutdown();
  return snap;
}

const identity = (b) => b;
const SERVING = Buffer.from([0x08, 0x01]); // HealthCheckResponse{status: SERVING}

function method(pathName, responseStream) {
  return {
    path: pathName,
    requestStream: false,
    responseStream,
    requestSerialize: identity,
    requestDeserialize: identity,
    responseSerialize: identity,
    responseDeserialize: identity,
  };
}

async function grpcCall(grpc, target) {
  const server = new grpc.Server();
  const unary = (_call, cb) => cb(null, SERVING);
  server.addService(
    { Check: method('/grpc.health.v1.Health/Check', false), Watch: method('/grpc.health.v1.Health/Watch', true) },
    {
      Check: unary,
      // One update, then the stream completes (as the Python driver's generator).
      Watch: (call) => {
        call.write(SERVING);
        call.end();
      },
    },
  );
  server.addService({ Ping: method('/parity.v1.Echo/Ping', false) }, { Ping: unary });
  const port = await new Promise((resolve, reject) => {
    server.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (err, p) => (err ? reject(err) : resolve(p)));
  });
  const client = new grpc.Client(`127.0.0.1:${port}`, grpc.credentials.createInsecure());
  try {
    if (target.endsWith('/Watch')) {
      await new Promise((resolve, reject) => {
        const call = client.makeServerStreamRequest(target, identity, identity, Buffer.alloc(0));
        call.on('data', () => {});
        call.on('end', resolve);
        call.on('error', reject);
      });
    } else {
      await new Promise((resolve, reject) => {
        client.makeUnaryRequest(target, identity, identity, Buffer.alloc(0), (err) => (err ? reject(err) : resolve()));
      });
    }
  } finally {
    client.close();
    await new Promise((resolve) => server.tryShutdown(() => resolve()));
  }
}

async function kindGrpcServer(c) {
  const snap = {};
  need(helper(), ['metricsHandler'], 'B7');
  const { exporter, options } = spanRecorder();
  setupCase(c, snap, options);
  if ('error' in snap) return snap;
  // Required after setup on purpose: the instrumentation patches it on load.
  const grpc = require('@grpc/grpc-js');
  try {
    await grpcCall(grpc, c.input?.method ?? '');
    snap.call_ok = true;
  } catch (err) {
    snap.call_ok = false;
    captured.push(`grpc call failed: ${err?.message ?? String(err)}`);
  }
  snap.server_spans = serverSpans(exporter);
  snap.rpcs_counted = await scrapeCount(/^rpc_server(_call)?_duration[a-z_]*_count[{ ]/);
  await shutdown();
  return snap;
}

async function kindLogging(c) {
  // `console` (stdout + stderr) is filled in by the parent from the process output.
  const snap = {};
  setupCase(c, snap);
  const h = helper();
  need(h, ['createLogger'], 'B8');
  h.createLogger({ name: 'parity' }).warn('parity-console-record');
  await shutdown();
  return snap;
}

async function kindMetricsScrape(c) {
  const snap = {};
  setupCase(c, snap);
  if ('error' in snap) return snap;
  need(helper(), ['metricsHandler'], 'B7');
  api().metrics.getMeter('parity').createCounter('parity.requests').add(1);
  const { status, text } = await scrape();
  snap.scrape = text;
  snap.scrape_status = status;
  await shutdown();
  return snap;
}

// Prefork model (design D-JS13): cluster.fork() twice; each worker sets up
// and reports its service.instance.id over IPC.
async function forkWorker(c) {
  const snap = {};
  try {
    installSink();
    setupCase(c, snap);
    snap.instance_id = resourceSnapshot()?.['service.instance.id'] ?? null;
    await shutdown();
  } catch (err) {
    if (err instanceof Pending) snap.adapter_pending = err.message;
    else if (err instanceof AdapterError) snap.adapter_error = err.message;
    else snap.crash = err?.stack ?? String(err);
  }
  snap.diag = captured;
  snap.helper = helperRecords;
  process.send(snap, () => process.exit(0));
}

async function kindFork(c) {
  need(helper(), ['setup', 'shutdown'], 'B3');
  mapOptions(c.code);
  cluster.setupPrimary({ exec: __filename, args: process.argv.slice(2), silent: false });
  const workers = [];
  for (let i = 0; i < 2; i++) {
    workers.push(
      new Promise((resolve, reject) => {
        const w = cluster.fork({ PARITY_FORK_WORKER: '1' });
        let msg;
        w.on('message', (m) => {
          msg = m;
        });
        w.on('exit', (code) => (msg ? resolve(msg) : reject(new Error(`cluster worker exited (code ${code}) without a report`))));
      }),
    );
  }
  const reports = await Promise.all(workers);
  const snap = {};
  const ids = [];
  for (const w of reports) {
    if (w.adapter_pending) throw new Pending(w.adapter_pending);
    if (w.adapter_error) throw new AdapterError(w.adapter_error);
    if (w.crash) throw new Error(`cluster worker crashed: ${w.crash}`);
    ids.push(w.instance_id ?? null);
    captured.push(...w.diag);
    helperRecords.push(...w.helper);
    if ('error' in w) snap.error = w.error;
  }
  snap.instance_ids = ids;
  snap.instance_ids_distinct = !ids.includes(null) && new Set(ids).size === ids.length;
  return snap;
}

const KINDS = {
  resolve: kindResolve,
  export: kindExport,
  sampler: kindSampler,
  propagation: kindPropagation,
  http_server: kindHttpServer,
  grpc_server: kindGrpcServer,
  logging: kindLogging,
  metrics_scrape: kindMetricsScrape,
  fork: kindFork,
};

async function runCase(c) {
  const kind = KINDS[c.kind];
  if (kind === undefined) return { adapter_error: `unknown kind ${JSON.stringify(c.kind)}` };
  try {
    installSink();
    const snap = await kind(c);
    snap.diagnostics = captured.join('\n');
    snap.helper_log = helperRecords.join('\n');
    return snap;
  } catch (err) {
    if (err instanceof Pending) return { adapter_pending: err.message };
    if (err instanceof AdapterError) return { adapter_error: err.message };
    throw err;
  }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length !== 2) {
    process.stderr.write('usage: node driver.cjs <case.json> <out.json> (spawned by runner.test.cjs; it is not a test file)\n');
    process.exit(2);
  }
  const [caseFile, outFile] = args;
  const c = JSON.parse(fs.readFileSync(caseFile, 'utf8'));
  if (cluster.isWorker && process.env.PARITY_FORK_WORKER === '1') {
    await forkWorker(c);
    return;
  }
  let snap;
  try {
    snap = await runCase(c);
  } catch (err) {
    // A driver crash: no out file, so the parent reports the stderr tail.
    process.stderr.write(`parity driver crashed: ${err?.stack ?? String(err)}\n`);
    process.exit(3);
  }
  fs.writeFileSync(outFile, JSON.stringify(snap));
  process.exit(0);
}

if (require.main === module) {
  main().catch((err) => {
    process.stderr.write(`parity driver crashed: ${err?.stack ?? String(err)}\n`);
    process.exit(3);
  });
}

module.exports = { CODE_KEYS, KINDS, classify, mapOptions, grpcTimeoutMs, signalOf, DRIVER: path.resolve(__filename) };
