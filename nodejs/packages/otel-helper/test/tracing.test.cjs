'use strict';
// Sampler, health sampler, propagators and the debug marker (task B5):
// contract R6, R7, R8, R9; contract design D1 rows "Sampler" and
// "Propagators", D3, D5; ADR-002, ADR-006; Node design D-JS3, D-JS7. The
// parity fixture covers the resolved behavior (sampler.*, debug.*,
// propagation.*); these tests cover the branches it does not reach, and the
// health sampler directly (its HTTP/gRPC wiring is task B6).
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('@opentelemetry/api');
const { InMemorySpanExporter, SimpleSpanProcessor, SamplingDecision } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const tracing = require('../dist/internal/tracing.js');
const { registeredGlobal } = require('../dist/internal/globals.js');
const { runCjs, lastJson } = require('./helpers.cjs');

const TRACE_ID = '0af7651916cd43dd8448eb211c80319c';
const SPAN_ID = 'b7ad6b7169203331';

let records;
let saved;

test.beforeEach(() => {
  saved = { ...process.env };
  for (const k of Object.keys(process.env)) {
    if (k.startsWith('OTEL_') || k === 'SERVICE_NAME' || k === 'ENVIRONMENT') delete process.env[k];
  }
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0'; // never bind :9464 from unit tests (test/metrics.test.cjs binds its own)
  process.env.OTEL_METRICS_EXPORTER = 'none';
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

function collector() {
  const seen = [];
  return { seen, warn: (message, attrs) => seen.push({ message, ...attrs }) };
}

function remote(sampled, traceState) {
  return api.trace.setSpanContext(api.ROOT_CONTEXT, {
    traceId: TRACE_ID,
    spanId: SPAN_ID,
    traceFlags: sampled ? api.TraceFlags.SAMPLED : api.TraceFlags.NONE,
    isRemote: true,
    traceState,
  });
}

function local(sampled) {
  return api.trace.setSpanContext(api.ROOT_CONTEXT, {
    traceId: TRACE_ID,
    spanId: SPAN_ID,
    traceFlags: sampled ? api.TraceFlags.SAMPLED : api.TraceFlags.NONE,
  });
}

// ── sampler resolution (R6, D3) ──────────────────────────────────────────

test('R6 default sampler is parentbased_always_on, silently', () => {
  const { seen, warn } = collector();
  assert.deepEqual(tracing.resolveSampler({}, warn), { name: 'parentbased_always_on', ratio: undefined });
  assert.deepEqual(seen, []);
});

test('R6 the six names are trimmed and case-insensitive; ratio samplers read _ARG', () => {
  const cases = [
    [' Always_On ', undefined, { name: 'always_on', ratio: undefined }],
    ['ALWAYS_OFF', undefined, { name: 'always_off', ratio: undefined }],
    ['parentbased_always_off', undefined, { name: 'parentbased_always_off', ratio: undefined }],
    ['parentbased_always_on', '0.5', { name: 'parentbased_always_on', ratio: undefined }],
    ['traceidratio', '0.25', { name: 'traceidratio', ratio: 0.25 }],
    ['parentbased_traceidratio', undefined, { name: 'parentbased_traceidratio', ratio: 1 }],
    ['parentbased_traceidratio', ' 0 ', { name: 'parentbased_traceidratio', ratio: 0 }],
  ];
  for (const [name, arg, want] of cases) {
    process.env.OTEL_TRACES_SAMPLER = name;
    if (arg === undefined) delete process.env.OTEL_TRACES_SAMPLER_ARG;
    else process.env.OTEL_TRACES_SAMPLER_ARG = arg;
    const { seen, warn } = collector();
    assert.deepEqual(tracing.resolveSampler({}, warn), want, name);
    assert.deepEqual(seen, [], name);
  }
});

test('R6/D3 invalid _ARG warns and uses 1.0 (no hex, sign, exponent, NaN, > 1)', () => {
  process.env.OTEL_TRACES_SAMPLER = 'traceidratio';
  for (const arg of ['0x1', '-0', '1e-1', 'NaN', '1.5', '.5', '１']) {
    process.env.OTEL_TRACES_SAMPLER_ARG = arg;
    const { seen, warn } = collector();
    assert.deepEqual(tracing.resolveSampler({}, warn), { name: 'traceidratio', ratio: 1 }, arg);
    assert.equal(seen.length, 1, arg);
    assert.equal(seen[0].variable, 'OTEL_TRACES_SAMPLER_ARG');
    assert.equal(seen[0].using, '1.0');
  }
});

test('R6 unknown and unsupported sampler names warn and use the default', () => {
  for (const [name, message] of [['bogus', /unknown sampler/], ['jaeger_remote', /unsupported by this helper/], ['XRAY', /unsupported/]]) {
    process.env.OTEL_TRACES_SAMPLER = name;
    const { seen, warn } = collector();
    assert.deepEqual(tracing.resolveSampler({}, warn), { name: 'parentbased_always_on', ratio: undefined });
    assert.equal(seen.length, 1);
    assert.match(seen[0].message, message);
    assert.equal(seen[0].variable, 'OTEL_TRACES_SAMPLER');
  }
});

test('R6 deprecated OTEL_HELPER_SAMPLE_RATIO: parentbased_traceidratio, only while OTEL_TRACES_SAMPLER is unset', () => {
  process.env.OTEL_HELPER_SAMPLE_RATIO = '0.1';
  assert.deepEqual(tracing.resolveSampler({}, collector().warn), { name: 'parentbased_traceidratio', ratio: 0.1 });
  process.env.OTEL_TRACES_SAMPLER = 'always_off';
  assert.deepEqual(tracing.resolveSampler({}, collector().warn), { name: 'always_off', ratio: undefined });
  delete process.env.OTEL_TRACES_SAMPLER;
  process.env.OTEL_HELPER_SAMPLE_RATIO = '2';
  const { seen, warn } = collector();
  assert.deepEqual(tracing.resolveSampler({}, warn), { name: 'parentbased_always_on', ratio: undefined });
  assert.equal(seen[0].variable, 'OTEL_HELPER_SAMPLE_RATIO');
});

test('R6/D1 a code sampleRatio wins over the env, 1 included', () => {
  process.env.OTEL_TRACES_SAMPLER = 'always_off';
  process.env.OTEL_HELPER_SAMPLE_RATIO = '0';
  assert.deepEqual(tracing.resolveSampler({ sampleRatio: 1 }, collector().warn), { name: 'parentbased_traceidratio', ratio: 1 });
});

test('R6 buildSampler: ParentBased for the default and the parentbased_* names', () => {
  const desc = (name, ratio) => tracing.buildSampler({ name, ratio }).toString();
  assert.match(desc('parentbased_always_on'), /^ParentBased\{root=AlwaysOnSampler/);
  assert.match(desc('parentbased_always_off'), /^ParentBased\{root=AlwaysOffSampler/);
  assert.match(desc('parentbased_traceidratio', 0.5), /^ParentBased\{root=TraceIdRatioBased\{0\.5\}/);
  assert.equal(desc('traceidratio', 0.25), 'TraceIdRatioBased{0.25}');
  assert.equal(desc('traceidratio', undefined), 'TraceIdRatioBased{1}');
  assert.equal(desc('always_on'), 'AlwaysOnSampler');
  assert.equal(desc('always_off'), 'AlwaysOffSampler');
});

test('R8 code options: sampleRatio and healthPaths fail fast, naming the option', () => {
  const bad = [
    [{ sampleRatio: 1.5 }, /sampleRatio must be a number in \[0, 1\], got 1\.5/],
    [{ sampleRatio: -0.1 }, /sampleRatio/],
    [{ sampleRatio: Number.NaN }, /got NaN/],
    [{ sampleRatio: '0.5' }, /sampleRatio must be a number in \[0, 1\], got string/],
    [{ healthPaths: '/health' }, /healthPaths must be an array of strings, got string/],
    [{ healthPaths: [1] }, /healthPaths item #1 must be a string, got number/],
    [{ healthPaths: ['/ok', 'health'] }, /healthPaths item #2 \("health"\) must be ASCII/],
    [{ healthPaths: ['/h?x'] }, /no query/],
    [{ healthPaths: ['/h#x'] }, /fragment/],
    [{ healthPaths: ['/a,b'] }, /comma/],
    [{ healthPaths: ['/héalth'] }, /ASCII/],
  ];
  for (const [options, message] of bad) assert.throws(() => helper.setup(options), message, JSON.stringify(options));
});

test('R6/R7 Resolved carries the sampler, propagators and health paths', () => {
  const r = helper.setup({ sampleRatio: 0.5, healthPaths: ['/Up/'] });
  assert.deepEqual(r.sampler, { name: 'parentbased_traceidratio', ratio: 0.5 });
  assert.deepEqual(r.propagators, ['tracecontext', 'baggage']);
  assert.deepEqual(r.healthPaths, ['/Up/']);
  assert.ok(Object.isFrozen(r.healthPaths));
});

test('R7 default health paths in Resolved', () => {
  assert.deepEqual(helper.setup().healthPaths, ['/health', '/healthz', '/ready', '/readyz', '/live', '/livez', '/ping', '/metrics']);
});

test('R6 the installed sampler honors a code ratio of 0 for roots and the parent for children', () => {
  helper.setup({ sampleRatio: 0 });
  const tracer = api.trace.getTracer('t');
  const root = tracer.startSpan('root', {}, api.ROOT_CONTEXT);
  assert.equal(root.isRecording(), false);
  const child = tracer.startSpan('child', {}, remote(true));
  assert.equal(child.isRecording(), true);
  child.end();
});

// ── health sampler (R7, D5, D-JS7) ───────────────────────────────────────

class Recorder {
  constructor() {
    this.calls = 0;
  }
  shouldSample() {
    this.calls++;
    return { decision: SamplingDecision.RECORD_AND_SAMPLED };
  }
  toString() {
    return 'Recorder';
  }
}

function decide(sampler, { ctx = api.ROOT_CONTEXT, name = 'GET', kind = api.SpanKind.SERVER, attributes = {} } = {}) {
  return sampler.shouldSample(ctx, TRACE_ID, name, kind, attributes, []);
}

test('R7 SERVER entry spans of health paths are dropped (no parent or a remote one)', () => {
  const inner = new Recorder();
  const s = new tracing.HealthCheckSampler(inner);
  for (const path of ['/health', '/Health', '/healthz/', '/ready?full=1', '/live#x', '/metrics', '/PING//']) {
    assert.equal(decide(s, { attributes: { 'url.path': path } }).decision, SamplingDecision.NOT_RECORD, path);
  }
  const ts = api.createTraceState('vendor=1');
  const r = decide(s, { ctx: remote(true, ts), attributes: { 'url.path': '/ready' } });
  assert.equal(r.decision, SamplingDecision.NOT_RECORD);
  assert.equal(r.traceState, ts, 'the parent tracestate is kept on a drop');
  assert.equal(decide(s, { attributes: { 'url.path': '/ready' } }).traceState, undefined);
  assert.equal(inner.calls, 0);
});

test('R7 http.target is the fallback; url.path wins', () => {
  const s = new tracing.HealthCheckSampler(new Recorder());
  assert.equal(decide(s, { attributes: { 'http.target': '/health?x=1' } }).decision, SamplingDecision.NOT_RECORD);
  assert.equal(decide(s, { attributes: { 'url.path': '/users', 'http.target': '/health' } }).decision, SamplingDecision.RECORD_AND_SAMPLED);
  assert.equal(decide(s, { attributes: { 'url.path': 42 } }).decision, SamplingDecision.RECORD_AND_SAMPLED);
});

test('R7 real routes, local parents and other kinds go to the inner sampler', () => {
  const inner = new Recorder();
  const s = new tracing.HealthCheckSampler(inner);
  for (const path of ['/users/ping', '/healthcare', '/', '/api/health']) {
    assert.equal(decide(s, { attributes: { 'url.path': path } }).decision, SamplingDecision.RECORD_AND_SAMPLED, path);
  }
  assert.equal(decide(s, { ctx: local(true), attributes: { 'url.path': '/health' } }).decision, SamplingDecision.RECORD_AND_SAMPLED);
  assert.equal(decide(s, { kind: api.SpanKind.INTERNAL, attributes: { 'url.path': '/health' } }).decision, SamplingDecision.RECORD_AND_SAMPLED);
  // HTTP clients are excluded by the instrumentation hooks (task B6), not by the sampler.
  assert.equal(decide(s, { kind: api.SpanKind.CLIENT, attributes: { 'url.path': '/health' } }).decision, SamplingDecision.RECORD_AND_SAMPLED);
  assert.equal(inner.calls, 7);
});

test('R7/D-JS7 gRPC health: the semconv span name (A30); SERVER entry spans and CLIENT spans with any parent', () => {
  const s = new tracing.HealthCheckSampler(new Recorder());
  for (const name of ['grpc.health.v1.Health/Check', 'grpc.health.v1.Health/Watch', 'grpc.health.v1.Health/List']) {
    assert.equal(decide(s, { name }).decision, SamplingDecision.NOT_RECORD, name);
    assert.equal(decide(s, { name, ctx: remote(true) }).decision, SamplingDecision.NOT_RECORD, name);
    assert.equal(decide(s, { name, kind: api.SpanKind.CLIENT, ctx: local(true) }).decision, SamplingDecision.NOT_RECORD, name);
  }
  assert.equal(decide(s, { name: 'grpc.health.v1.Health/Check', ctx: local(true) }).decision, SamplingDecision.RECORD_AND_SAMPLED);
  // A user service in package health.v1 (span health.v1.Health/<method>) is kept; so are other names and rpc.* attributes.
  for (const name of ['health.v1.Health/Watch', 'health.v1.Health/Check', '/grpc.health.v1.Health/List', 'notification.Notifier/Send', 'healthz/Check', 'grpc.health.v1.HealthX/Check']) {
    assert.equal(decide(s, { name }).decision, SamplingDecision.RECORD_AND_SAMPLED, name);
    assert.equal(decide(s, { name, kind: api.SpanKind.CLIENT }).decision, SamplingDecision.RECORD_AND_SAMPLED, name);
  }
  const attrs = { 'rpc.method': '/grpc.health.v1.Health/Check', 'rpc.service': 'grpc.health.v1.Health' };
  assert.equal(decide(s, { name: 'x/Y', attributes: attrs }).decision, SamplingDecision.RECORD_AND_SAMPLED);
  assert.equal(tracing.isGrpcHealthSpanName('grpc.health.v1.Health/Check'), true);
  assert.equal(tracing.isGrpcHealthSpanName('health.v1.Health/Check'), false);
  // An instrumentation-grpc registered outside the helper keeps upstream's name (review L2, 2026-11-08).
  assert.equal(tracing.isGrpcHealthSpanName('grpc.grpc.health.v1.Health/Check'), true);
  assert.equal(decide(s, { name: 'grpc.grpc.health.v1.Health/Check' }).decision, SamplingDecision.NOT_RECORD);
});

test('A30 gRPC span names: grpc.<service>/<method> becomes <service>/<method>; other names are kept', () => {
  for (const [name, want] of [
    ['grpc.parity.v1.Echo/Ping', 'parity.v1.Echo/Ping'],
    ['grpc.grpc.health.v1.Health/Check', 'grpc.health.v1.Health/Check'],
    ['grpc.Svc/M', 'Svc/M'],
    ['grpc.', 'grpc.'],
    ['grpc./M', 'grpc./M'],
    ['grpc.Svc/', 'grpc.Svc/'],
    ['grpc.noslash', 'grpc.noslash'],
    ['parity.v1.Echo/Ping', 'parity.v1.Echo/Ping'],
    ['GET /x', 'GET /x'],
  ]) {
    assert.equal(tracing.grpcSemconvSpanName(name), want, name);
  }
  const exporter = new InMemorySpanExporter();
  const { TracerProvider } = require('@opentelemetry/sdk-trace');
  const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  const wrapped = tracing.grpcNamingTracerProvider(provider);
  const tracer = wrapped.getTracer('@opentelemetry/instrumentation-grpc');
  tracer.startSpan('grpc.parity.v1.Echo/Ping').end();
  tracer.startActiveSpan('grpc.parity.v1.Echo/Active', (span) => span.end());
  tracer.startActiveSpan('grpc.parity.v1.Echo/Opts', {}, (span) => span.end());
  assert.deepEqual(exporter.getFinishedSpans().map((s) => s.name), ['parity.v1.Echo/Ping', 'parity.v1.Echo/Active', 'parity.v1.Echo/Opts']);
  assert.equal(typeof wrapped.forceFlush, 'function');
});

test('R7/R9 an invalid parent context counts as no parent (health sampler and debug marker)', () => {
  const invalid = api.trace.setSpan(api.ROOT_CONTEXT, api.trace.wrapSpanContext(api.INVALID_SPAN_CONTEXT));
  const s = new tracing.HealthCheckSampler(new Recorder());
  const r = decide(s, { ctx: invalid, attributes: { 'url.path': '/health' } });
  assert.equal(r.decision, SamplingDecision.NOT_RECORD);
  assert.equal(r.traceState, undefined);
  const exporter = new InMemorySpanExporter();
  helper.setup({ debug: true, spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  api.trace.getTracer('t').startSpan('entry', {}, invalid).end();
  assert.equal(exporter.getFinishedSpans()[0].attributes.debug, 'true');
});

test('R7 healthPaths replaces the list; [] traces every HTTP path but still drops gRPC health', () => {
  const custom = new tracing.HealthCheckSampler(new Recorder(), ['/Up/']);
  assert.equal(custom.isHealthPath('/up'), true);
  assert.equal(custom.isHealthPath('/health'), false);
  const none = new tracing.HealthCheckSampler(new Recorder(), []);
  assert.equal(decide(none, { attributes: { 'url.path': '/health' } }).decision, SamplingDecision.RECORD_AND_SAMPLED);
  assert.equal(decide(none, { name: 'grpc.health.v1.Health/Check' }).decision, SamplingDecision.NOT_RECORD);
  assert.equal(none.toString(), 'HealthCheckExcluding{Recorder}');
});

test('R7/D-JS7 the health sampler wraps outside ParentBased: a probe with a sampled traceparent is dropped, its children too', () => {
  const exporter = new InMemorySpanExporter();
  helper.setup({ spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  const tracer = api.trace.getTracer('t');
  const server = tracer.startSpan('GET', { kind: api.SpanKind.SERVER, attributes: { 'url.path': '/health' } }, remote(true));
  assert.equal(server.isRecording(), false);
  const child = tracer.startSpan('db', {}, api.trace.setSpan(remote(true), server));
  assert.equal(child.isRecording(), false);
  const real = tracer.startSpan('GET', { kind: api.SpanKind.SERVER, attributes: { 'url.path': '/users' } }, remote(true));
  assert.equal(real.isRecording(), true);
  real.end();
  assert.equal(exporter.getFinishedSpans().length, 1);
});

test('R7 under always_on the children of a dropped health span are sampled again (documented)', () => {
  process.env.OTEL_TRACES_SAMPLER = 'always_on';
  helper.setup();
  const tracer = api.trace.getTracer('t');
  const server = tracer.startSpan('GET', { kind: api.SpanKind.SERVER, attributes: { 'url.path': '/health' } }, api.ROOT_CONTEXT);
  assert.equal(server.isRecording(), false);
  assert.equal(tracer.startSpan('db', {}, api.trace.setSpan(api.ROOT_CONTEXT, server)).isRecording(), true);
});

test('normalizeHealthPath keeps "/" and strips query, fragment and trailing slashes', () => {
  assert.equal(tracing.normalizeHealthPath('/'), '/');
  assert.equal(tracing.normalizeHealthPath('///'), '/');
  assert.equal(tracing.normalizeHealthPath('/A/B/?q#f'), '/a/b');
  assert.equal(tracing.normalizeHealthPath('/x#y?z'), '/x');
  // A20: ASCII-only folding; U+212A (Kelvin sign) is not 'k'.
  assert.equal(tracing.normalizeHealthPath('/HEALTH'), '/health');
  assert.equal(tracing.normalizeHealthPath('/\u212aube'), '/\u212aube');
  const s = new tracing.HealthCheckSampler(new Recorder(), ['/kube']);
  assert.equal(s.isHealthPath('/KUBE'), true);
  assert.equal(s.isHealthPath('/\u212aube'), false);
});

// ── propagators (R8, D1) ─────────────────────────────────────────────────

test('R8 OTEL_PROPAGATORS: default, subsets, casing, empty tokens, duplicates', () => {
  const cases = [
    [undefined, ['tracecontext', 'baggage']],
    ['  ', ['tracecontext', 'baggage']],
    ['baggage', ['baggage']],
    ['TraceContext', ['tracecontext']],
    [',baggage,,TRACECONTEXT,baggage,', ['baggage', 'tracecontext']],
    ['none', []],
    [' NONE ', []],
  ];
  for (const [raw, want] of cases) {
    if (raw === undefined) delete process.env.OTEL_PROPAGATORS;
    else process.env.OTEL_PROPAGATORS = raw;
    const { seen, warn } = collector();
    assert.deepEqual(tracing.resolvePropagators(warn), want, String(raw));
    assert.deepEqual(seen, [], String(raw));
  }
});

test('R8/A20 unsupported and unknown propagators warn per token and are skipped; nothing left → the default', () => {
  process.env.OTEL_PROPAGATORS = 'b3,Jaeger,bogus';
  const { seen, warn } = collector();
  assert.deepEqual(tracing.resolvePropagators(warn), ['tracecontext', 'baggage']);
  assert.deepEqual(seen.map((s) => [s.message, s.value, s.using]), [
    ['propagator not supported by the Node.js helper, ignored', 'b3', 'tracecontext, baggage'],
    ['propagator not supported by the Node.js helper, ignored', 'jaeger', 'tracecontext, baggage'],
    ['unknown propagator, ignored', 'bogus', 'tracecontext, baggage'],
  ]);
  assert.ok(seen.every((s) => s.variable === 'OTEL_PROPAGATORS'));
});

test('A20 a propagator warning names the effective list as using', () => {
  process.env.OTEL_PROPAGATORS = 'foo,baggage,xray';
  const { seen, warn } = collector();
  assert.deepEqual(tracing.resolvePropagators(warn), ['baggage']);
  assert.deepEqual(seen.map((s) => [s.value, s.using]), [['foo', 'baggage'], ['xray', 'baggage']]);
});

test('A20 "none" with other propagators warns, and every other token still warns', () => {
  process.env.OTEL_PROPAGATORS = 'tracecontext,none,b3';
  const { seen, warn } = collector();
  assert.deepEqual(tracing.resolvePropagators(warn), []);
  assert.deepEqual(seen.map((s) => [s.message, s.using]), [
    ['propagator not supported by the Node.js helper, ignored', 'none'],
    ['"none" combined with other propagators; propagation disabled', 'none'],
  ]);
  process.env.OTEL_PROPAGATORS = 'none,tracecontext';
  const two = collector();
  assert.deepEqual(tracing.resolvePropagators(two.warn), []);
  assert.equal(two.seen.length, 1);
});

test('A20 env enum values fold ASCII only (U+212A, U+0130, U+0131, U+017F are not letters of a name)', () => {
  const { asciiLower, asciiUpper } = require('../dist/internal/env.js');
  assert.equal(asciiLower('PARENTBASED_TRACE\u0130DRATIO'), 'parentbased_trace\u0130dratio');
  assert.equal(asciiUpper('\u0131nfo'), '\u0131NFO');
  process.env.OTEL_PROPAGATORS = 'BAGGAGE,tracecontext\u212a';
  const p = collector();
  assert.deepEqual(tracing.resolvePropagators(p.warn), ['baggage']);
  assert.equal(p.seen.length, 1);
  process.env.OTEL_TRACES_SAMPLER = 'PARENTBASED_TRACE\u0130DRATIO';
  const s = collector();
  assert.deepEqual(tracing.resolveSampler({}, s.warn), { name: 'parentbased_always_on', ratio: undefined });
  assert.equal(s.seen.length, 1);
  process.env.OTEL_SDK_DISABLED = 'fal\u017fe';
  assert.equal(helper.setup().sdkDisabled, false);
  assert.ok(records.some((r) => r.line.includes('unparseable boolean')));
});

test('R8 buildPropagator: [] injects nothing; the W3C pair injects traceparent and baggage', () => {
  const ctx = api.propagation.setBaggage(local(true), api.propagation.createBaggage({ k: { value: 'v' } }));
  const carrier = {};
  tracing.buildPropagator(['tracecontext', 'baggage']).inject(ctx, carrier, api.defaultTextMapSetter);
  assert.deepEqual(Object.keys(carrier).sort(), ['baggage', 'traceparent']);
  const empty = {};
  tracing.buildPropagator([]).inject(ctx, empty, api.defaultTextMapSetter);
  assert.deepEqual(empty, {});
});

function injected() {
  const carrier = {};
  const ctx = api.propagation.setBaggage(api.context.active(), api.propagation.createBaggage({ k: { value: 'v' } }));
  api.context.with(ctx, () => {
    api.trace.getTracer('t').startActiveSpan('s', (span) => {
      api.propagation.inject(api.context.active(), carrier);
      span.end();
    });
  });
  return Object.keys(carrier).sort();
}

test('D1 the propagators are installed under OTEL_SDK_DISABLED=true', () => {
  process.env.OTEL_SDK_DISABLED = 'true';
  process.env.OTEL_PROPAGATORS = 'tracecontext,baggage';
  helper.setup();
  assert.deepEqual(injected(), ['baggage']);
});

test('D-JS1 the propagator is released by shutdown/reset and installed again by the next setup', async () => {
  process.env.OTEL_PROPAGATORS = 'baggage';
  helper.setup();
  assert.deepEqual(injected(), ['baggage']);
  await helper.shutdown();
  testing.reset();
  delete process.env.OTEL_PROPAGATORS;
  helper.setup();
  assert.deepEqual(injected(), ['baggage', 'traceparent']);
});

test('D-JS10 a propagator registered by someone else is kept and reported', () => {
  const foreign = { inject: (_c, carrier) => { carrier.foreign = '1'; }, extract: (c) => c, fields: () => ['foreign'] };
  assert.equal(api.propagation.setGlobalPropagator(foreign), true);
  try {
    helper.setup();
    assert.ok(records.some((r) => r.line.includes('a global propagator is already registered; the helper keeps it and OTEL_PROPAGATORS is not applied')));
    assert.deepEqual(injected(), ['foreign']);
  } finally {
    api.propagation.disable();
  }
});

test('D-JS10 an API refusing the global propagator is reported', () => {
  const res = runCjs(`
    const key = Symbol.for('opentelemetry.js.api.1');
    const { loadedApiVersion } = require('./dist/internal/globals.js');
    const reg = { version: loadedApiVersion() };
    let reads = 0;
    Object.defineProperty(reg, 'propagation', { get: () => (reads++ === 0 ? undefined : {}), set: () => {}, configurable: true });
    globalThis[key] = reg;
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l, m) => { if (m.origin === 'helper' && l.includes('propagator')) lines.push(l); });
    process.env.OTEL_METRICS_EXPORTER = 'none';
    require('@staffops/otel-helper').setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false });
    console.log(JSON.stringify(lines));
  `);
  assert.deepEqual(lastJson(res), ['otelhelper: the OpenTelemetry API refused the global propagator; context is not propagated across processes']);
});

// ── debug marker (R9, ADR-006) ───────────────────────────────────────────

function spansWith(options) {
  const exporter = new InMemorySpanExporter();
  helper.setup({ ...options, spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  const tracer = api.trace.getTracer('t');
  const root = tracer.startSpan('root', {}, api.ROOT_CONTEXT);
  tracer.startSpan('child', {}, api.trace.setSpan(api.ROOT_CONTEXT, root)).end();
  root.end();
  tracer.startSpan('entry', {}, remote(true)).end();
  return Object.fromEntries(exporter.getFinishedSpans().map((s) => [s.name, s.attributes.debug]));
}

test('R9 debug mode marks entry spans (no parent or a remote one), never local children', () => {
  assert.deepEqual(spansWith({ debug: true }), { child: undefined, root: 'true', entry: 'true' });
});

test('R9 OTEL_HELPER_DEBUG_LEVEL=true turns the marker on; off by default', async () => {
  assert.deepEqual(spansWith({}), { child: undefined, root: undefined, entry: undefined });
  await helper.shutdown();
  testing.reset();
  process.env.OTEL_HELPER_DEBUG_LEVEL = 'true';
  assert.deepEqual(spansWith({}), { child: undefined, root: 'true', entry: 'true' });
});

test('R9 the marker processor is inert outside onStart', async () => {
  const p = new tracing.DebugMarkerProcessor();
  p.onEnd();
  await p.forceFlush();
  await p.shutdown();
});

test('D1/A20 OTEL_SDK_DISABLED: the sampler variables are not read; the INFO line says whether a propagator was installed', () => {
  process.env.OTEL_SDK_DISABLED = 'true';
  process.env.OTEL_TRACES_SAMPLER = 'bogus';
  const r = helper.setup();
  assert.deepEqual(r.sampler, { name: 'parentbased_always_on', ratio: undefined });
  assert.ok(!records.some((x) => x.line.includes('OTEL_TRACES_SAMPLER')));
  assert.ok(records.some((x) => x.level === 'info' && x.line.includes('only propagators installed')));
});

test('D1/A20 OTEL_SDK_DISABLED with a foreign propagator: the INFO line does not claim one was installed', () => {
  process.env.OTEL_SDK_DISABLED = 'true';
  const foreign = { inject: () => undefined, extract: (c) => c, fields: () => [] };
  api.propagation.setGlobalPropagator(foreign);
  try {
    helper.setup();
    assert.ok(records.some((x) => x.level === 'info' && x.line.includes('telemetry disabled; the helper installed no propagator')));
    assert.ok(!records.some((x) => x.line.includes('only propagators installed')));
  } finally {
    api.propagation.disable();
  }
});

test('R8 a rejected setup() registers no propagator', () => {
  for (const options of [{ sampleRatio: 2 }, { healthPaths: ['x'] }]) {
    assert.throws(() => helper.setup(options), TypeError);
    assert.equal(registeredGlobal('propagation'), undefined, JSON.stringify(options));
  }
});

test('D-JS10 shutdown → setup applies a changed OTEL_PROPAGATORS', async () => {
  process.env.OTEL_PROPAGATORS = 'baggage';
  helper.setup();
  assert.deepEqual(injected(), ['baggage']);
  await helper.shutdown();
  process.env.OTEL_PROPAGATORS = 'tracecontext';
  helper.setup();
  assert.deepEqual(injected(), ['traceparent']);
});

test('D-JS10 re-setup releases only the helper\'s own propagator', async () => {
  helper.setup();
  await helper.shutdown();
  // Someone replaces the helper's propagator before the next setup.
  api.propagation.disable();
  const foreign = { inject: (_c, carrier) => { carrier.foreign = '1'; }, extract: (c) => c, fields: () => ['foreign'] };
  api.propagation.setGlobalPropagator(foreign);
  try {
    helper.setup();
    assert.ok(records.some((r) => r.line.includes("the global propagator is no longer the helper's; the helper keeps it")));
    assert.deepEqual(injected(), ['foreign']);
  } finally {
    api.propagation.disable();
  }
});

// ── disabled traces: a no-op tracer (task A35; contract D1 row "Propagation with traces off") ──

/** A span processor recording the names of the spans it sees. */
function probeProcessor() {
  const started = [];
  const probe = { started, shutdowns: 0, onStart: (span) => started.push(span.name), onEnd() {}, forceFlush: () => Promise.resolve() };
  probe.shutdown = () => { probe.shutdowns++; return Promise.resolve(); };
  return probe;
}

/** Starts a span under `parent` (default: the active context) with baggage; returns the injected carrier and the span. */
function injectUnder(parent = api.context.active()) {
  const carrier = {};
  const ctx = api.propagation.setBaggage(parent, api.propagation.createBaggage({ k: { value: 'v' } }));
  let recording;
  let ids;
  api.context.with(ctx, () => {
    api.trace.getTracer('t').startActiveSpan('s', (span) => {
      recording = span.isRecording();
      ids = require('../dist/internal/logging.js').spanIds(api.context.active());
      api.propagation.inject(api.context.active(), carrier);
      span.end();
    });
  });
  return { carrier, recording, ids };
}

for (const [label, env, code] of [
  ['OTEL_HELPER_DISABLED_SIGNALS=traces', { OTEL_HELPER_DISABLED_SIGNALS: 'traces' }, {}],
  ['code disabledSignals: [traces]', {}, { disabledSignals: ['traces'] }],
]) {
  test(`A35 ${label}: a root carries no context, baggage still propagates, no span reaches a processor`, async () => {
    Object.assign(process.env, env);
    const probe = probeProcessor();
    const resolved = helper.setup({ ...code, spanProcessors: [probe] });
    assert.deepEqual(resolved.tracesExporters, []);
    assert.deepEqual(resolved.inactiveSignals, []);
    const fw = api.trace.getTracerProvider().getDelegate();
    assert.equal(fw.constructor.name, 'ForwardingTracerProvider');
    assert.equal(fw.getDelegate(), undefined);
    const root = injectUnder();
    assert.deepEqual(Object.keys(root.carrier).sort(), ['baggage']);
    assert.equal(root.recording, false);
    // Log correlation (core logger, -pino, -winston): no ids without a valid span context.
    assert.equal(root.ids, undefined);
    assert.deepEqual(probe.started, []);
    // The app's processor is still owned by the (unused) SDK provider and shut down with it.
    await helper.shutdown();
    assert.equal(probe.shutdowns, 1);
  });
}

test('A35 tracesDisabled: only the disabled set, not OTEL_SDK_DISABLED, a code [] or an unknown standard entry', () => {
  const { resolveSignals } = require('../dist/internal/signals.js');
  const NO_OTLP = Object.freeze({ traces: undefined, metrics: undefined, logs: undefined });
  const quiet = () => undefined;
  process.env.OTEL_HELPER_DISABLED_SIGNALS = 'traces';
  assert.equal(resolveSignals({}, NO_OTLP, quiet).tracesDisabled, true);
  // Code disabledSignals wins over the env, also [].
  assert.equal(resolveSignals({ disabledSignals: [] }, NO_OTLP, quiet).tracesDisabled, false);
  // An unknown entry of the standard variable counts as unset: the deprecated one applies.
  process.env.OTEL_TRACES_EXPORTER = 'bogus';
  assert.equal(resolveSignals({}, NO_OTLP, quiet).tracesDisabled, true);
  process.env.OTEL_TRACES_EXPORTER = 'none';
  assert.equal(resolveSignals({}, NO_OTLP, quiet).tracesDisabled, false);
  delete process.env.OTEL_TRACES_EXPORTER;
  // OTEL_SDK_DISABLED=true builds no provider at all: not "traces disabled".
  process.env.OTEL_SDK_DISABLED = 'true';
  assert.equal(resolveSignals({}, NO_OTLP, quiet).tracesDisabled, false);
  // Carried on the Plan.
  delete process.env.OTEL_SDK_DISABLED;
  const { resolvePlan } = require('../dist/internal/resolve.js');
  assert.equal(resolvePlan({}, 'svc', true).tracesDisabled, true);
  assert.equal(resolvePlan({ disabledSignals: [] }, 'svc', true).tracesDisabled, false);
});

test('A35 disabled traces pass an inbound trace context on unchanged (traceparent and tracestate)', () => {
  process.env.OTEL_HELPER_DISABLED_SIGNALS = 'traces';
  helper.setup();
  for (const sampled of [true, false]) {
    const inbound = remote(sampled, api.createTraceState('vendor=a,other=b'));
    const out = injectUnder(inbound);
    assert.equal(out.carrier.traceparent, `00-${TRACE_ID}-${SPAN_ID}-0${sampled ? 1 : 0}`);
    assert.equal(out.carrier.tracestate, 'vendor=a,other=b');
    assert.ok(out.carrier.baggage);
    assert.equal(out.recording, false);
    assert.deepEqual(out.ids, { traceId: TRACE_ID, spanId: SPAN_ID });
  }
  // A root option never inherits the inbound context.
  const carrier = {};
  api.context.with(remote(true), () => {
    const span = api.trace.getTracer('t').startSpan('r', { root: true });
    api.propagation.inject(api.trace.setSpan(api.ROOT_CONTEXT, span), carrier);
  });
  assert.deepEqual(carrier, {});
});

test('A35 OTEL_TRACES_EXPORTER=none is not disabled: an SDK provider samples roots and injects their context', () => {
  process.env.OTEL_TRACES_EXPORTER = 'none';
  // The standard variable wins over the deprecated one (D1 row "Deprecated OTEL_HELPER_DISABLED_SIGNALS").
  process.env.OTEL_HELPER_DISABLED_SIGNALS = 'traces';
  const probe = probeProcessor();
  const resolved = helper.setup({ spanProcessors: [probe] });
  assert.deepEqual(resolved.tracesExporters, []);
  assert.notEqual(api.trace.getTracerProvider().getDelegate().getDelegate(), undefined);
  const root = injectUnder();
  assert.deepEqual(Object.keys(root.carrier).sort(), ['baggage', 'traceparent']);
  assert.match(root.carrier.traceparent, /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  assert.equal(root.recording, true);
  assert.notEqual(root.ids, undefined);
  assert.deepEqual(probe.started, ['s']);
});

test('A35 a re-setup with traces enabled gives the forwarder its SDK provider back, and a disabled one removes it', async () => {
  process.env.OTEL_HELPER_DISABLED_SIGNALS = 'traces';
  helper.setup();
  const tracer = api.trace.getTracer('t');
  assert.equal(tracer.startSpan('a').isRecording(), false);
  await helper.shutdown();
  delete process.env.OTEL_HELPER_DISABLED_SIGNALS;
  helper.setup();
  assert.equal(tracer.startSpan('b').isRecording(), true);
  await helper.shutdown();
  helper.setup({ disabledSignals: ['traces'] });
  assert.equal(tracer.startSpan('c').isRecording(), false);
  assert.deepEqual(Object.keys(injectUnder().carrier).sort(), ['baggage']);
});

test('A35 OTEL_SDK_DISABLED=true with traces disabled: no provider, roots carry no context', () => {
  process.env.OTEL_SDK_DISABLED = 'true';
  process.env.OTEL_HELPER_DISABLED_SIGNALS = 'traces';
  helper.setup();
  assert.equal(testing.getProviders(), undefined);
  assert.deepEqual(Object.keys(injectUnder().carrier).sort(), ['baggage']);
});

test('A35 disabled traces with a foreign TracerProvider: kept and listed without replaceGlobals; replaced by the no-op forwarder with it', async () => {
  process.env.OTEL_HELPER_DISABLED_SIGNALS = 'traces';
  const { TracerProvider } = require('@opentelemetry/sdk-trace');
  const foreign = new TracerProvider();
  api.trace.setGlobalTracerProvider(foreign);
  const httpInstrumentation = () => testing.getState().runtime.instrumentations.instrumentations[0];
  try {
    const kept = helper.setup();
    assert.deepEqual(kept.inactiveSignals, ['traces']);
    assert.equal(api.trace.getTracerProvider().getDelegate(), foreign);
    // The instrumentations join the app's own provider, as with traces enabled.
    assert.equal(httpInstrumentation().tracer.constructor.name, 'Tracer');
    assert.deepEqual(Object.keys(injectUnder().carrier).sort(), ['baggage', 'traceparent']);
    await helper.shutdown();
    const replaced = helper.setup({ replaceGlobals: true });
    assert.deepEqual(replaced.inactiveSignals, []);
    const fw = api.trace.getTracerProvider().getDelegate();
    assert.equal(fw.constructor.name, 'ForwardingTracerProvider');
    assert.equal(fw.getDelegate(), undefined);
    assert.equal(httpInstrumentation().tracer.constructor.name, 'ForwardingTracer');
    assert.deepEqual(Object.keys(injectUnder().carrier).sort(), ['baggage']);
  } finally {
    testing.reset();
    api.trace.disable();
  }
});

test('A35 disabled traces: an HTTP server root sends no traceparent downstream; an inbound one is passed on unchanged', async () => {
  const res = runCjs(`
    process.env.OTEL_HELPER_DISABLED_SIGNALS = 'traces';
    process.env.OTEL_METRICS_EXPORTER = 'prometheus';
    process.env.OTEL_HELPER_METRICS_PORT = '0';
    const helper = require('@staffops/otel-helper');
    helper.setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false });
    const http = require('node:http');
    const seen = [];
    const downstream = http.createServer((req, res) => { seen.push({ traceparent: req.headers.traceparent, tracestate: req.headers.tracestate }); res.end('ok'); });
    downstream.listen(0, '127.0.0.1', () => {
      const dport = downstream.address().port;
      const front = http.createServer((req, res) => {
        http.get({ host: '127.0.0.1', port: dport, path: '/' }, (r) => { r.resume(); r.on('end', () => res.end('ok')); });
      });
      front.listen(0, '127.0.0.1', async () => {
        const fport = front.address().port;
        const call = (headers) => new Promise((resolve) => http.get({ host: '127.0.0.1', port: fport, path: '/', headers }, (r) => { r.resume(); r.on('end', resolve); }));
        await call({});
        await call({ traceparent: '00-${TRACE_ID}-${SPAN_ID}-01', tracestate: 'vendor=a' });
        front.close(); downstream.close();
        // The HTTP metrics are recorded with traces disabled: 2 front + 2 downstream server requests.
        await new Promise((r) => setTimeout(r, 50));
        const scrape = { statusCode: 0, headers: {}, setHeader(k, v) { this.headers[k] = v; }, writeHead(c, h) { this.statusCode = c; Object.assign(this.headers, h || {}); return this; }, end(body) { this.body = String(body); this.done(); } };
        const text = await new Promise((resolve) => { scrape.done = () => resolve(scrape.body); helper.metricsHandler()({ method: 'GET', url: '/metrics', headers: {} }, scrape); });
        const count = text.split('\\n').filter((l) => /^http_server_request_duration_seconds_count[{ ]/.test(l)).reduce((n, l) => n + Number(l.trim().split(/\\s+/).pop()), 0);
        console.log(JSON.stringify({ seen, count }));
      });
    });
  `);
  assert.deepEqual(lastJson(res), { seen: [{}, { traceparent: `00-${TRACE_ID}-${SPAN_ID}-01`, tracestate: 'vendor=a' }], count: 4 });
});
