'use strict';
// OTLP endpoint, protocol and TLS-setting resolution (task B4a): contract R1,
// R2, R8, design D1, D1.a–D1.c and the D1 edge-case rulings; Node design D-JS3
// (ownership table) and D-JS14 (IPv6 zone ids). Exporters are task B4b, so
// these tests read the resolved plan (`Resolved.otlp`) and the diagnostics.
const test = require('node:test');
const assert = require('node:assert/strict');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { redactURL, UNPARSEABLE_URL } = require('../dist/internal/otlp.js');
const { runCjs, lastJson } = require('./helpers.cjs');

let records;
let saved;

function resetAndCapture() {
  testing.reset();
  records = [];
  testing.setDiagnosticSink((line, meta) => records.push({ line, ...meta }));
}

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

/** setup() with `env` applied; returns Resolved.otlp. */
function plan(env = {}, options = {}) {
  Object.assign(process.env, env);
  return helper.setup({ ...options }).otlp;
}

/** One fresh setup per call (the "once per resolution" warnings start again). */
function fresh(env, options) {
  testing.reset();
  resetAndCapture();
  for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_EXPORTER_OTLP')) delete process.env[k];
  return plan(env, options);
}

test('R2 no endpoint anywhere: no signal is OTLP-configured, nothing is reported about OTLP', () => {
  const otlp = plan();
  assert.deepEqual(otlp, { traces: undefined, metrics: undefined, logs: undefined });
  assert.ok(Object.isFrozen(otlp));
  assert.deepEqual(warnings(), []);
});

test('R1 generic gRPC endpoint configures every signal: target host:port, plaintext from http://', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_ENDPOINT: ' http://collector:4317 \n' });
  for (const signal of ['traces', 'metrics', 'logs']) {
    assert.deepEqual(otlp[signal], {
      protocol: 'grpc',
      url: 'http://collector:4317',
      grpcTarget: 'collector:4317',
      insecure: true,
      source: 'OTEL_EXPORTER_OTLP_ENDPOINT',
      ignoredTlsVariables: [],
    });
    assert.ok(Object.isFrozen(otlp[signal]));
  }
  assert.deepEqual(warnings(), []);
});

test('R2 a per-signal endpoint alone configures only that signal (no silent localhost export)', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'https://traces.example.com:4317' });
  assert.equal(otlp.traces.url, 'https://traces.example.com:4317');
  assert.equal(otlp.traces.insecure, false);
  assert.equal(otlp.metrics, undefined);
  assert.equal(otlp.logs, undefined);
});

test('R1/R2 http/protobuf: generic gets <prefix>/v1/<signal>, per-signal keeps its path verbatim', () => {
  const otlp = plan({
    OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
    OTEL_EXPORTER_OTLP_ENDPOINT: 'http://collector:4318/otlp/',
    OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: 'http://logs:4318/custom/logs',
  });
  assert.equal(otlp.traces.url, 'http://collector:4318/otlp/v1/traces');
  assert.equal(otlp.metrics.url, 'http://collector:4318/otlp/v1/metrics');
  assert.equal(otlp.logs.url, 'http://logs:4318/custom/logs');
  assert.equal(otlp.logs.source, 'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT');
  assert.equal(otlp.traces.grpcTarget, undefined);
  // A per-signal URL without a path stays without one; a generic one gets /v1/<signal>.
  const bare = fresh({ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_EXPORTER_OTLP_ENDPOINT: 'https://c', OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: 'https://m' });
  assert.equal(bare.traces.url, 'https://c/v1/traces');
  assert.equal(bare.metrics.url, 'https://m');
});

test('R1 http/protobuf URL without a port keeps the scheme default (443/80), no warning', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_EXPORTER_OTLP_ENDPOINT: 'https://otlp.example.com' });
  assert.equal(otlp.traces.url, 'https://otlp.example.com/v1/traces');
  assert.deepEqual(warnings(), []);
  // An explicit default port is kept as written.
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:80' }).traces.url, 'http://c:80/v1/traces');
});

test('D1 precedence: code per-signal > code generic > env per-signal > env generic', () => {
  const env = { OTEL_EXPORTER_OTLP_ENDPOINT: 'http://env-generic:4317', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://env-traces:4317' };
  let otlp = fresh(env);
  assert.equal(otlp.traces.grpcTarget, 'env-traces:4317');
  assert.equal(otlp.metrics.grpcTarget, 'env-generic:4317');
  otlp = fresh(env, { otelEndpoint: 'http://code-generic:4317' });
  assert.equal(otlp.traces.grpcTarget, 'code-generic:4317');
  assert.equal(otlp.traces.source, 'otelEndpoint');
  otlp = fresh(env, { otelEndpoint: 'http://code-generic:4317', metricsEndpoint: 'http://code-metrics:4317' });
  assert.equal(otlp.metrics.grpcTarget, 'code-metrics:4317');
  assert.equal(otlp.metrics.source, 'metricsEndpoint');
  // An empty (or blank) code endpoint is not set.
  otlp = fresh(env, { otelEndpoint: '  ', tracesEndpoint: '' });
  assert.equal(otlp.traces.grpcTarget, 'env-traces:4317');
});

test('D1 rulings: an unparseable per-signal env endpoint warns and falls back to the generic one', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://generic:4317', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://:4317' });
  assert.equal(otlp.traces.grpcTarget, 'generic:4317');
  assert.deepEqual(warnings(), [
    'otelhelper: invalid endpoint; ignored variable=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT value="<unparseable URL>" using="next configured endpoint, else none"',
  ]);
});

test('D1 rulings (2026-10-29): a port outside 1–65535 is invalid — env warns <unparseable URL> and falls back, code fails fast', () => {
  for (const value of ['http://collector:70000', 'http://collector:0', 'collector:0', 'collector:65536']) {
    const otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://generic:4317', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: value, OTEL_EXPORTER_OTLP_INSECURE: 'true' });
    assert.equal(otlp.traces.grpcTarget, 'generic:4317', value);
    assert.equal(warnings().length, 1, value);
    assert.match(warnings()[0], /^otelhelper: invalid endpoint.*; ignored variable=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT value="<unparseable URL>" /, value);
    assert.throws(() => fresh({}, { otelEndpoint: value }), /^TypeError: setup\(\): otelEndpoint invalid endpoint.*: "<unparseable URL>"$/, value);
  }
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:65535' }).traces.grpcTarget, 'c:65535');
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:1' }).traces.grpcTarget, 'c:1');
});

test('D1 rulings (2026-10-29): an unparseable endpoint is always shown as the constant, never partly', () => {
  for (const value of ['http://:4317', 'collector:abc', 'collector:4317:9']) {
    fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: value });
    assert.deepEqual(warnings().map((l) => l.replace(/^otelhelper: [^;]*; ignored /, '')), [
      'variable=OTEL_EXPORTER_OTLP_ENDPOINT value="<unparseable URL>" using="next configured endpoint, else none"',
    ], value);
  }
});

test('D1 rulings: an unsupported scheme (also opaque, without //) leaves the signal unconfigured', () => {
  for (const value of ['unix:///tmp/otlp.sock', 'ftp://c:4317', 'unix:/tmp/x', 'collector:/v1/traces']) {
    const otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://generic:4317', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: value });
    assert.equal(otlp.traces, undefined, value);
    assert.equal(otlp.metrics.grpcTarget, 'generic:4317', value);
    assert.equal(warnings().length, 1, value);
    assert.match(warnings()[0], /^otelhelper: unsupported endpoint scheme \(use http:\/\/ or https:\/\/\); ignored variable=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT .* using="no OTLP exporter for this signal"$/);
  }
});

test('R1 schemeless host:port: https:// + default port; http:// only with INSECURE=true; gRPC not deprecated', () => {
  let otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'collector' });
  assert.equal(otlp.traces.url, 'https://collector:4317');
  assert.equal(otlp.traces.insecure, false);
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'collector:4317', OTEL_EXPORTER_OTLP_INSECURE: 'TRUE' });
  assert.equal(otlp.traces.url, 'http://collector:4317');
  assert.equal(otlp.traces.grpcTarget, 'collector:4317');
  assert.equal(otlp.traces.insecure, true);
  assert.deepEqual(lines(), []);
  // Schemeless with an explicit :80 keeps it (and a scheme-default port never reads as "no port").
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'collector:80', OTEL_EXPORTER_OTLP_INSECURE: 'true' }).logs.grpcTarget, 'collector:80');
  // host:port must have a numeric port.
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'collector:abc' });
  assert.equal(otlp.traces, undefined);
  assert.match(warnings()[0], /invalid endpoint; ignored variable=OTEL_EXPORTER_OTLP_ENDPOINT/);
});

test('D8 schemeless http/protobuf endpoint: deprecation warned once per process, port defaulted to 4318', () => {
  let otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'collector', OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf' });
  assert.equal(otlp.traces.url, 'https://collector:4318/v1/traces');
  const dep = 'otelhelper: schemeless endpoint is deprecated for http/protobuf and will be removed in 1.0; use a URL (http:// or https://) variable=OTEL_EXPORTER_OTLP_ENDPOINT value=collector using=https://collector:4318';
  assert.deepEqual(warnings(), [dep]);
  // Once per process: a later setup in the same process stays silent.
  void helper.shutdown();
  records.length = 0;
  otlp = plan();
  assert.equal(otlp.logs.url, 'https://collector:4318/v1/logs');
  assert.deepEqual(warnings(), []);
});

test('D1.b portless gRPC URL: port 4317 + one warning recommending an explicit port (http and https)', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_ENDPOINT: 'https://collector' });
  assert.equal(otlp.traces.url, 'https://collector:4317');
  assert.equal(otlp.traces.grpcTarget, 'collector:4317');
  // Once per resolution, not once per signal.
  assert.deepEqual(warnings(), [
    'otelhelper: gRPC endpoint has no port; using 4317 — set an explicit port (e.g. :4317, or :443 behind an ingress) variable=OTEL_EXPORTER_OTLP_ENDPOINT value=https://collector using=https://collector:4317',
  ]);
});

test('D-JS3 gRPC http://h:80 and https://h:443 keep the written port (WHATWG URL.host drops it)', () => {
  let otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://collector:80' });
  assert.equal(otlp.traces.grpcTarget, 'collector:80');
  assert.equal(otlp.traces.url, 'http://collector:80');
  assert.equal(otlp.traces.insecure, true);
  assert.deepEqual(warnings(), []);
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'https://collector:443' });
  assert.equal(otlp.metrics.grpcTarget, 'collector:443');
  assert.equal(otlp.metrics.insecure, false);
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://collector:0080' });
  assert.equal(otlp.logs.grpcTarget, 'collector:80');
});

test('R8 userinfo, query and fragment are removed and never echoed', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://u:s3cret-pw@collector:4317/?token=s3cret-q#frag' });
  assert.equal(otlp.traces.url, 'http://collector:4317');
  assert.equal(otlp.traces.grpcTarget, 'collector:4317');
  assert.deepEqual(warnings(), [
    'otelhelper: endpoint userinfo/query is not supported and was removed; put credentials in OTEL_EXPORTER_OTLP_HEADERS variable=OTEL_EXPORTER_OTLP_ENDPOINT value=http://collector:4317/ using=http://collector:4317',
  ]);
  // An empty query or a bare '@' counts too; http/protobuf strips them from the request URL.
  const http = fresh({ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'https://@c:4318/v1/t?' });
  assert.equal(http.traces.url, 'https://c:4318/v1/t');
  assert.equal(warnings().length, 1);
  assert.ok(!lines().join('\n').includes('s3cret'));
});

test('R8 passwords with "/" or "#" never reach diagnostics (unparseable or host only)', () => {
  for (const value of ['http://user:s3cret/pw@collector:4317', 'http://user:s3cret#pw@collector:4317', 'http://user:s3cret?pw@collector:4317']) {
    const otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: value });
    assert.equal(otlp.traces, undefined, value);
    const text = lines().join('\n');
    assert.match(text, /OTEL_EXPORTER_OTLP_ENDPOINT/, value);
    assert.ok(!text.includes('s3cret'), text);
  }
});

test('D1 rulings: a control character inside an endpoint is unparseable (<unparseable URL>) and falls back', () => {
  for (const value of ['http://collector:4317/\tx', 'http://coll\nector:4317', 'unix:/x\u007f']) {
    const otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://generic:4317', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: value });
    assert.equal(otlp.traces.grpcTarget, 'generic:4317', JSON.stringify(value));
    assert.deepEqual(warnings(), [
      'otelhelper: endpoint contains a control character; ignored variable=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT value="<unparseable URL>" using="next configured endpoint, else none"',
    ]);
  }
});

test('D1 ruling RFC 3986 (BLOCKING B1): a backslash anywhere is invalid; the password never leaks', () => {
  for (const value of ['http://user:12\\s3cret@collector:4317', 'http://collector:4317\\v1', 'collector\\x:4317']) {
    const otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://generic:4317', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: value });
    assert.equal(otlp.traces.grpcTarget, 'generic:4317', value);
    assert.deepEqual(warnings(), [
      'otelhelper: endpoint contains a backslash; ignored variable=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT value="<unparseable URL>" using="next configured endpoint, else none"',
    ], value);
    assert.throws(() => fresh({}, { otelEndpoint: value }), (e) => /^setup\(\): otelEndpoint endpoint contains a backslash: "<unparseable URL>"$/.test(e.message) && !e.message.includes('s3cret'));
  }
  assert.equal(redactURL('http://user:12\\s3cret@collector:4317'), UNPARSEABLE_URL);
});

test('D1 ruling RFC 3986: a scheme needs // and a non-empty host; a schemeless value cannot start with /', () => {
  for (const value of ['http:///collector:4317', 'https://@:4317', '//collector:4317', '/collector:4317', 'http://[::1:4317']) {
    const otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://generic:4317', OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: value });
    assert.equal(otlp.logs.grpcTarget, 'generic:4317', value);
    assert.match(warnings()[0], /^otelhelper: invalid endpoint; ignored variable=OTEL_EXPORTER_OTLP_LOGS_ENDPOINT value="<unparseable URL>"/, value);
    assert.throws(() => fresh({}, { logsEndpoint: value }), /^TypeError: setup\(\): logsEndpoint invalid endpoint: "<unparseable URL>"$/, value);
  }
});

test('D1 ruling RFC 3986: userinfo is cut at the last @ of the authority; WHATWG must agree on host and port', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://us@er:pa@ss@collector:4317' });
  assert.equal(otlp.traces.grpcTarget, 'collector:4317');
  assert.ok(!lines().join('\n').includes('pa@ss'));
});

test('D1 ruling: leading-zero ports compare numerically (4318 inference, numeric rendering)', () => {
  const otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://collector:04318' });
  assert.equal(otlp.traces.protocol, 'http/protobuf');
  assert.equal(otlp.traces.url, 'http://collector:4318/v1/traces');
  assert.equal(fresh({}, { otelEndpoint: 'collector:004318', insecure: true }).metrics.url, 'http://collector:4318/v1/metrics');
});

test('D1 ruling: path escapes are kept verbatim, never decoded or rejected', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4318/a%2Fb/%zz', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://c:4318/x%2Fy/../%7e' });
  assert.equal(otlp.metrics.url, 'http://c:4318/a%2Fb/%zz/v1/metrics');
  assert.equal(otlp.traces.url, 'http://c:4318/x%2Fy/../%7e');
  assert.deepEqual(warnings(), []);
});

test('D1 ruling (host characters): a percent-encoded byte or a space in the host is invalid', () => {
  for (const value of ['http://%63ollector:4317', 'http://coll%20ector:4317', 'http://coll ector:4317', '%63ollector:4317']) {
    const otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://generic:4317', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: value });
    assert.equal(otlp.traces.grpcTarget, 'generic:4317', value);
    assert.match(warnings()[0], /^otelhelper: invalid endpoint; ignored .*value="<unparseable URL>"/, value);
    assert.throws(() => fresh({}, { tracesEndpoint: value }), /invalid endpoint: "<unparseable URL>"$/, value);
  }
});

test('D1 ruling: an empty ? or # is removed with the userinfo/query warning, once per variable', () => {
  for (const value of ['http://c:4317/?', 'http://c:4317#', 'http://c:4317?#']) {
    const otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: value });
    assert.equal(otlp.traces.url, 'http://c:4317', value);
    assert.equal(warnings().filter((l) => l.includes('userinfo/query')).length, 1, value);
  }
  const http = fresh({ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_EXPORTER_OTLP_ENDPOINT: 'http://u:p@c:4318/base?' });
  assert.equal(http.logs.url, 'http://c:4318/base/v1/logs');
  // Once per variable and value, not once per signal (the `using` differs per signal).
  assert.deepEqual(warnings(), [
    'otelhelper: endpoint userinfo/query is not supported and was removed; put credentials in OTEL_EXPORTER_OTLP_HEADERS variable=OTEL_EXPORTER_OTLP_ENDPOINT value=http://c:4318/base using=http://c:4318/base/v1/traces',
  ]);
});

test("D1 ruling: an '@' in the path or query warns naming the variable only; the endpoint is used as parsed", () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://user:4317/s3cret@collector' });
  assert.equal(otlp.traces.url, 'http://user:4317/s3cret@collector');
  assert.deepEqual(warnings(), ["otelhelper: endpoint path contains '@' — misplaced credentials? variable=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]);
  fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4317/?k=s3cret@x' });
  assert.ok(warnings().includes("otelhelper: endpoint path contains '@' — misplaced credentials? variable=OTEL_EXPORTER_OTLP_ENDPOINT"));
  assert.ok(!lines().join('\n').includes('s3cret'));
});

test('D-JS14: WHATWG host normalizations remain (lowercase, punycode, IPv4 shorthand, IPv4-mapped IPv6)', () => {
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://COLLECTOR.Example:4317' }).traces.grpcTarget, 'collector.example:4317');
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://bücher.example:4317' }).traces.grpcTarget, 'xn--bcher-kva.example:4317');
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://127.1:4317' }).traces.grpcTarget, '127.0.0.1:4317');
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://[::ffff:127.0.0.1]:4317' }).traces.grpcTarget, '[::ffff:7f00:1]:4317');
});

test('truncateValue cuts by code point (a surrogate pair is never split)', () => {
  const { truncateValue } = require('../dist/internal/diagnostics.js');
  assert.equal(truncateValue(`${'a'.repeat(63)}😀😀`), `${'a'.repeat(63)}😀…`);
  assert.equal(truncateValue('😀'.repeat(64)), '😀'.repeat(64));
  assert.throws(() => helper.setup({ otlpProtocol: 'x'.repeat(100) }), (e) => e.message.includes(`"${'x'.repeat(64)}…"`));
});

test('inactive copy (another helper version owns the process): otlp still resolved, no endpoint warnings', () => {
  const res = runCjs(`
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'), { value: { version: '9.9.9', hooks: undefined } });
    process.env.OTEL_SERVICE_NAME = 'svc';
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'https://u:p@collector';
    process.env.OTEL_EXPORTER_OTLP_INSECURE = 'yes';
    process.env.OTEL_HELPER_DEBUG_LEVEL = 'maybe';
    const r = require('@staffops/otel-helper').setup();
    console.log(JSON.stringify(r.otlp.traces));
  `);
  assert.equal(lastJson(res).grpcTarget, 'collector:4317');
  assert.deepEqual(res.stderr.trim().split('\n').map((l) => l.replace(/ loaded=.*$/, '')), [
    'otelhelper: another version is already loaded in this process; this copy of @staffops/otel-helper stays inactive',
  ]);
});

test('R1 IPv6 literals keep their brackets (URL, schemeless, HTTP)', () => {
  let otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://[::1]:4317' });
  assert.equal(otlp.traces.grpcTarget, '[::1]:4317');
  assert.equal(otlp.traces.url, 'http://[::1]:4317');
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: '[2001:db8::1]', OTEL_EXPORTER_OTLP_INSECURE: 'true' });
  assert.equal(otlp.traces.grpcTarget, '[2001:db8::1]:4317');
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://[::1]:24318', OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf' });
  assert.equal(otlp.metrics.url, 'http://[::1]:24318/v1/metrics');
});

test('D1 rulings (host characters): IPv6 zone ids are invalid — warn <unparseable URL>, fall back', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://generic:4317', OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: 'http://[fe80::1%25eth0]:4317' });
  assert.equal(otlp.logs.grpcTarget, 'generic:4317');
  assert.deepEqual(warnings(), [
    'otelhelper: IPv6 zone ids are not supported in endpoint URLs; ignored variable=OTEL_EXPORTER_OTLP_LOGS_ENDPOINT value="<unparseable URL>" using="next configured endpoint, else none"',
  ]);
  assert.throws(() => fresh({}, { otelEndpoint: 'http://[fe80::1%eth0]:4317' }), /^TypeError: setup\(\): otelEndpoint IPv6 zone ids/);
});

test('D1 protocol: code > _<SIGNAL>_PROTOCOL > OTEL_EXPORTER_OTLP_PROTOCOL > grpc (case-insensitive, trimmed)', () => {
  const env = { OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4317', OTEL_EXPORTER_OTLP_PROTOCOL: ' HTTP/Protobuf ', OTEL_EXPORTER_OTLP_LOGS_PROTOCOL: 'grpc' };
  let otlp = fresh(env);
  assert.equal(otlp.traces.protocol, 'http/protobuf');
  assert.equal(otlp.traces.url, 'http://c:4317/v1/traces');
  assert.equal(otlp.logs.protocol, 'grpc');
  otlp = fresh(env, { otlpProtocol: ' GRPC ' });
  assert.equal(otlp.traces.protocol, 'grpc');
  // An empty code protocol is not set (D1 ruling): the env decides.
  otlp = fresh(env, { otlpProtocol: '' });
  assert.equal(otlp.traces.protocol, 'http/protobuf');
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4317' }).traces.protocol, 'grpc');
});

test('D1.a port 4318 and no protocol: http/protobuf, INFO once per setup (per-signal endpoint decides)', () => {
  const otlp = plan({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4318', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://t:4317' });
  assert.equal(otlp.traces.protocol, 'grpc');
  assert.equal(otlp.metrics.protocol, 'http/protobuf');
  assert.equal(otlp.logs.url, 'http://c:4318/v1/logs');
  assert.deepEqual(records.map((r) => [r.origin, r.level, r.line]), [
    ['helper', 'info', 'otelhelper: endpoint port 4318 and no protocol set; using http/protobuf set=OTEL_EXPORTER_OTLP_PROTOCOL'],
  ]);
  // Schemeless works too; a protocol set anywhere disables the inference.
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'c:4318', OTEL_EXPORTER_OTLP_INSECURE: 'true' }).traces.url, 'http://c:4318/v1/traces');
  assert.equal(fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4318', OTEL_EXPORTER_OTLP_PROTOCOL: 'grpc' }).traces.protocol, 'grpc');
  assert.equal(lines((r) => r.level === 'info').length, 0);
});

test('D1 protocol env: http/json warns and is ignored; unknown values warn; next level applies', () => {
  let otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4317', OTEL_EXPORTER_OTLP_PROTOCOL: 'http/json' });
  assert.equal(otlp.traces.protocol, 'grpc');
  assert.deepEqual(warnings(), [
    'otelhelper: OTLP protocol http/json is not supported by the Node.js helper; ignored variable=OTEL_EXPORTER_OTLP_PROTOCOL value=http/json using="port inference, else grpc"',
  ]);
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://c:4318', OTEL_EXPORTER_OTLP_TRACES_PROTOCOL: 'kafka', OTEL_EXPORTER_OTLP_PROTOCOL: 'grpc' });
  assert.equal(otlp.traces.protocol, 'grpc');
  assert.deepEqual(warnings(), [
    'otelhelper: unknown OTLP protocol; ignored variable=OTEL_EXPORTER_OTLP_TRACES_PROTOCOL value=kafka using="port inference, else grpc"',
  ]);
});

test('D1 rulings INSECURE: boolean grammar, garbage warns and reads false; per-signal beats generic', () => {
  let otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'collector:4317', OTEL_EXPORTER_OTLP_INSECURE: 'yes' });
  assert.equal(otlp.traces.url, 'https://collector:4317');
  assert.deepEqual(warnings(), ['otelhelper: unparseable boolean variable=OTEL_EXPORTER_OTLP_INSECURE value=yes using=false']);
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'collector:4317', OTEL_EXPORTER_OTLP_INSECURE: 'true', OTEL_EXPORTER_OTLP_METRICS_INSECURE: 'false' });
  assert.equal(otlp.traces.insecure, true);
  assert.equal(otlp.metrics.insecure, false);
  // Code wins over env.
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'collector:4317', OTEL_EXPORTER_OTLP_INSECURE: 'false' }, { insecure: true });
  assert.equal(otlp.logs.url, 'http://collector:4317');
});

test('D1.c INSECURE contradicting the scheme warns once; the scheme wins (both directions)', () => {
  let otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'https://collector:4317', OTEL_EXPORTER_OTLP_INSECURE: 'true' });
  assert.equal(otlp.traces.insecure, false);
  assert.deepEqual(warnings(), [
    'otelhelper: INSECURE setting contradicts the endpoint scheme; the scheme wins variable=OTEL_EXPORTER_OTLP_INSECURE value=true using=https://',
  ]);
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://collector:4317', OTEL_EXPORTER_OTLP_TRACES_INSECURE: 'false' });
  assert.equal(otlp.traces.insecure, true);
  assert.deepEqual(warnings(), [
    'otelhelper: INSECURE setting contradicts the endpoint scheme; the scheme wins variable=OTEL_EXPORTER_OTLP_TRACES_INSECURE value=false using=http://',
  ]);
  // Code insecure with an env URL: warned, not fatal.
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'https://collector:4317' }, { insecure: true });
  assert.equal(otlp.traces.insecure, false);
  assert.match(warnings()[0], /contradicts the endpoint scheme; the scheme wins variable=insecure value=true/);
});

test('R2 TLS files with a plaintext endpoint: warned by name only, listed as ignored', () => {
  const otlp = plan({
    OTEL_EXPORTER_OTLP_ENDPOINT: 'http://collector:4317',
    OTEL_EXPORTER_OTLP_CERTIFICATE: '/x/ca.pem',
    OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY: '/x/s3cret-key.pem',
  });
  assert.deepEqual(otlp.traces.ignoredTlsVariables, ['OTEL_EXPORTER_OTLP_CERTIFICATE', 'OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY']);
  assert.deepEqual(otlp.metrics.ignoredTlsVariables, ['OTEL_EXPORTER_OTLP_CERTIFICATE']);
  assert.ok(Object.isFrozen(otlp.traces.ignoredTlsVariables));
  const text = warnings().join('\n');
  assert.match(text, /TLS files are set but the endpoint is plaintext .* variable=OTEL_EXPORTER_OTLP_CERTIFICATE,OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY value="<file paths not shown>"/);
  assert.ok(!text.includes('s3cret') && !text.includes('/x/'), text);
  // TLS endpoint: nothing ignored, nothing warned.
  const tls = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'https://collector:4317', OTEL_EXPORTER_OTLP_CERTIFICATE: '/x/ca.pem' });
  assert.deepEqual(tls.traces.ignoredTlsVariables, []);
  assert.deepEqual(warnings(), []);
});

test('R8 code values fail fast naming the option, before anything is installed, never echoing secrets', () => {
  const cases = [
    [{ otelEndpoint: 'https://collector:4317', insecure: true }, /^TypeError: setup\(\): insecure true contradicts the https:\/\/ scheme of otelEndpoint$/],
    [{ tracesEndpoint: 'http://collector:4317', insecure: false }, /^TypeError: setup\(\): insecure false contradicts the http:\/\/ scheme of tracesEndpoint$/],
    [{ otelEndpoint: 'unix:/tmp/sock' }, /^TypeError: setup\(\): otelEndpoint unsupported endpoint scheme/],
    [{ logsEndpoint: 'ftp://c:1' }, /^TypeError: setup\(\): logsEndpoint unsupported endpoint scheme \(use http:\/\/ or https:\/\/\): "ftp:\/\/c:1"$/],
    [{ otelEndpoint: 'http://u:s3cret/x@c:4317' }, /^TypeError: setup\(\): otelEndpoint invalid endpoint: "<unparseable URL>"$/],
    [{ otelEndpoint: 'http://c:4317\n/x' }, /^TypeError: setup\(\): otelEndpoint endpoint contains a control character/],
    [{ otlpProtocol: 'bogus' }, /^TypeError: setup\(\): otlpProtocol has unknown OTLP protocol "bogus"; valid values: grpc, http\/protobuf$/],
    [{ otlpProtocol: 'http/json' }, /otlpProtocol has unknown OTLP protocol "http\/json"/],
    [{ otelEndpoint: 4317 }, /^TypeError: setup\(\): otelEndpoint must be a string, got number$/],
    [{ otlpProtocol: null }, /^TypeError: setup\(\): otlpProtocol must be a string, got null$/],
    [{ insecure: 'true' }, /^TypeError: setup\(\): insecure must be a boolean, got string$/],
  ];
  for (const [options, re] of cases) {
    resetAndCapture();
    assert.throws(() => helper.setup(options), (err) => re.test(`${err.name}: ${err.message}`) && !err.message.includes('s3cret'), JSON.stringify(options));
    assert.equal(testing.getResource(), undefined);
    assert.deepEqual(lines(), [], JSON.stringify(options));
  }
});

test('R8 code: only the endpoint each signal uses is checked; a schemeless code endpoint follows code insecure', () => {
  // otelEndpoint is never used: every signal has its own code endpoint.
  const otlp = plan({}, {
    otelEndpoint: 'https://unused:4317',
    tracesEndpoint: 'http://t:4317',
    metricsEndpoint: 'http://m:4317',
    logsEndpoint: 'http://l:4317',
    insecure: true,
  });
  assert.deepEqual([otlp.traces.grpcTarget, otlp.metrics.grpcTarget, otlp.logs.grpcTarget], ['t:4317', 'm:4317', 'l:4317']);
  assert.equal(fresh({}, { otelEndpoint: 'collector:4317', insecure: true }).traces.url, 'http://collector:4317');
  assert.equal(fresh({}, { otelEndpoint: 'collector:4317', insecure: false }).traces.url, 'https://collector:4317');
});

test('Resolved keeps its B3 fields and adds otlp; the inactive-copy path stays silent', () => {
  const resolved = helper.setup({ otelEndpoint: 'http://c:4317' });
  assert.deepEqual(Object.keys(resolved).sort(), ['debug', 'environment', 'logLevel', 'otlp', 'serviceInstanceId', 'serviceName']);
  assert.ok(Object.isFrozen(resolved));
});

test('redactURL: userinfo, query and fragment removed; unparseable values become a constant', () => {
  assert.equal(redactURL('http://u:p@h:4317/x?q=1#f'), 'http://h:4317/x');
  assert.equal(redactURL('https://h'), 'https://h');
  assert.equal(redactURL('http://h:80'), 'http://h:80');
  assert.equal(redactURL('u:p@h:4317?q'), 'h:4317');
  assert.equal(redactURL('unix:///tmp/s'), 'unix:///tmp/s');
  for (const bad of ['', '   ', 'http://h\n/x', 'http://u:p/w@h:1', 'http://[fe80::1%25eth0]:1', '@?x', 'http://h:0', 'h:0', 'h:abc', 'h:70000']) assert.equal(redactURL(bad), UNPARSEABLE_URL, JSON.stringify(bad));
});

// The registry is process-global: these run in a fresh process.
test('globals: a registry of another @opentelemetry/api version is foreign — reported, nothing registered', () => {
  const { loadedApiVersion } = require('../dist/internal/globals.js');
  const apiVersion = loadedApiVersion();
  assert.match(apiVersion, /^1\.10\.[0-9]+$/);
  // Same minor, other patch: the API compares versions exactly, so it is foreign too.
  const otherPatch = apiVersion === '1.10.1' ? '1.10.0' : '1.10.1';
  const throwing = '{ get version() { throw new Error("boom"); } }';
  for (const [version, shown] of [['1.9.0', '1.9.0'], ['1.11.0', '1.11.0'], [otherPatch, otherPatch], [42, 'unknown'], [throwing, 'unknown']]) {
    const literal = version === throwing ? throwing : `{ version: ${JSON.stringify(version)} }`;
    const res = runCjs(`
      const key = Symbol.for('opentelemetry.js.api.1');
      globalThis[key] = ${literal};
      const helper = require('@staffops/otel-helper');
      const testing = require('@staffops/otel-helper/testing');
      const lines = [];
      testing.setDiagnosticSink((l) => lines.push(l));
      helper.setup({ serviceName: 'svc', environment: 'DEV' });
      console.log(JSON.stringify({ lines, keys: Object.keys(globalThis[key]).filter((k) => k !== 'version').concat('version').sort() }));
    `);
    const out = lastJson(res);
    assert.deepEqual(out.keys, ['version'], String(version));
    assert.deepEqual(out.lines, [
      `otelhelper: the OpenTelemetry API globals belong to another @opentelemetry/api version; the helper registers no global and its telemetry is inactive version=${shown} required=${apiVersion}`,
    ]);
  }
  // A supported registry (created by the installed 1.10.x API) is used as before.
  const res = runCjs(`
    const key = Symbol.for('opentelemetry.js.api.1');
    const fs = require('node:fs');
    const path = require('node:path');
    let dir = path.dirname(require.resolve('@opentelemetry/api'));
    while (!fs.existsSync(path.join(dir, 'package.json')) || require(path.join(dir, 'package.json')).name !== '@opentelemetry/api') dir = path.dirname(dir);
    globalThis[key] = { version: require(path.join(dir, 'package.json')).version };
    const helper = require('@staffops/otel-helper');
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
    helper.setup({ serviceName: 'svc', environment: 'DEV' });
    console.log(JSON.stringify(Object.keys(globalThis[key]).sort()));
  `);
  assert.deepEqual(lastJson(res), ['context', 'diag', 'trace', 'version']);
});

test('globals: a registration the API refuses anyway is reported (context manager, TracerProvider)', () => {
  const res = runCjs(`
    const key = Symbol.for('opentelemetry.js.api.1');
    const { loadedApiVersion } = require('./dist/internal/globals.js');
    const reg = { version: loadedApiVersion() };
    // Empty for the helper's check, taken for the API's duplicate check.
    for (const type of ['context', 'trace']) {
      let reads = 0;
      Object.defineProperty(reg, type, { get: () => (reads++ === 0 ? undefined : {}), set: () => {}, configurable: true });
    }
    globalThis[key] = reg;
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l, m) => { if (m.origin === 'helper') lines.push(l); });
    require('@staffops/otel-helper').setup({ serviceName: 'svc', environment: 'DEV', installDiagLogger: false });
    console.log(JSON.stringify(lines));
  `);
  assert.deepEqual(lastJson(res), [
    'otelhelper: the OpenTelemetry API refused the global context manager; in-process context is not propagated',
    "otelhelper: the OpenTelemetry API refused the global TracerProvider; the helper's traces are inactive signal=traces",
  ]);
});

test('R8 credentials placed in the path never reach a diagnostic or an error', () => {
  const inputs = [
    [{ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://user/s3cret@c' }, {}],
    [{ OTEL_EXPORTER_OTLP_ENDPOINT: 'user:123/s3cret@c', OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf' }, {}],
    [{ OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_EXPORTER_OTLP_ENDPOINT: 'http://u:p@c:4318/s3cret@x?' }, {}],
    [{ OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'ftp://user/s3cret@x' }, {}],
    [{ OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'unix:/u:s3cret@s' }, {}],
  ];
  for (const [env, options] of inputs) {
    testing.reset();
    const all = [];
    testing.setDiagnosticSink((line) => all.push(line));
    for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_EXPORTER_OTLP')) delete process.env[k];
    Object.assign(process.env, env);
    helper.setup(options);
    assert.ok(all.length > 0, JSON.stringify(env));
    for (const line of all) assert.ok(!line.includes('s3cret'), line);
  }
  for (const value of ['ftp://user/s3cret@x', 'unix:/u:s3cret@s', 'http://user/s3cret@c\\x']) {
    assert.throws(() => fresh({}, { otelEndpoint: value }), (e) => e instanceof TypeError && !e.message.includes('s3cret'), value);
  }
  assert.equal(redactURL('http://user/s3cret@c'), 'http://user');
  assert.equal(redactURL('http://h:1/p?k=s3cret@x'), 'http://h:1');
  assert.equal(redactURL('unix:/u:s3cret@s'), 's');
  assert.equal(redactURL('ftp://user/s3cret@x'), 'ftp://user');
});

test('D1 ruling (IDN): hosts are punycoded; a host that fails IDNA processing is invalid', () => {
  let otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://bücher.de' });
  assert.equal(otlp.traces.grpcTarget, 'xn--bcher-kva.de:4317');
  assert.match(warnings()[0], /using=http:\/\/xn--bcher-kva\.de:4317$/);
  otlp = fresh({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://generic:4317', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://�.example:4317' });
  assert.equal(otlp.traces.grpcTarget, 'generic:4317');
  assert.match(warnings()[0], /^otelhelper: invalid endpoint; ignored variable=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT value="<unparseable URL>"/);
  assert.throws(() => fresh({}, { otelEndpoint: 'http://�.example:4317' }), /otelEndpoint invalid endpoint: "<unparseable URL>"$/);
  assert.throws(() => fresh({}, { otelEndpoint: 'https://h:4318/x\\y' }), /otelEndpoint endpoint contains a backslash/);
});

