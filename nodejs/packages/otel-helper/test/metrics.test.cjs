'use strict';
// The Prometheus /metrics endpoint (task B7): Node design D-JS8 (listener,
// mounted handler, unit-suffix adapter), D-JS14 (busy port does not fail the
// synchronous setup; cluster workers share the port), contract design
// "Mounted /metrics handler status" and the D1 rows on the Prometheus
// fallback (no endpoint or `prometheus` in OTEL_METRICS_EXPORTER,
// OTEL_HELPER_METRICS_PORT 9464 / 0 = no listener), ADR-007. The parity
// fixture covers the scrape names (metrics.scrape_* cases).
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const api = require('@opentelemetry/api');
const { MeterProvider } = require('@opentelemetry/sdk-metrics');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const endpoint = require('../dist/internal/metrics-endpoint.js');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { once } = require('node:events');
const { runCjs, runNode, lastJson } = require('./helpers.cjs');
const { classify } = require('../../../test/parity/driver.cjs');

const { prometheusName, unitSuffix, resolveMetricsPort, checkCodeMetricsPort, serializeMetrics, CONTENT_TYPE, globMatch, droppingExporter, resolveDisabledMetrics } = endpoint;

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
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  process.env.OTEL_TRACES_EXPORTER = 'none';
  process.env.OTEL_LOGS_EXPORTER = 'none';
  resetAndCapture();
});

test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

const SUM = 3; // DataPointType.SUM
const GAUGE = 2;
const HISTOGRAM = 0;
const metric = (name, unit, dataPointType, isMonotonic) => ({ descriptor: { name, unit, description: '', valueType: 1 }, dataPointType, isMonotonic });

/** Calls the mounted handler with an in-memory response (the surface design D-JS8 allows). */
function mockScrape(req = { method: 'GET', url: '/metrics', headers: {} }, { headersSent = false } = {}) {
  const handler = helper.metricsHandler();
  return new Promise((resolve) => {
    const headers = {};
    const res = {
      statusCode: 200,
      headersSent,
      setHeader: (k, v) => {
        headers[k.toLowerCase()] = v;
      },
      end: (chunk) => resolve({ status: res.statusCode, headers, body: chunk === undefined ? undefined : String(chunk) }),
    };
    handler(req, res);
  });
}

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

function get(port, path, host = '127.0.0.1', method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = http.request({ host, port, path, method, agent: false }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (d) => {
        body += d;
      });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on('error', reject);
    req.end();
  });
}

/**
 * Settles once the helper's listener is bound or failed to bind (never a
 * fixed sleep): resolves with the bind error, or `undefined` once listening.
 */
async function listenerSettled() {
  const listener = testing.getState().runtime.active.listener;
  assert.ok(listener !== undefined, 'setup() started a listener');
  if (listener.listening) return undefined;
  const [err] = await Promise.race([once(listener, 'error'), once(listener, 'listening').then(() => [undefined])]);
  return err;
}

/**
 * `start(port)` on a port `freePort()` found, until the listener binds:
 * another process may take the port between the probe and the bind, so an
 * EADDRINUSE is retried once (with a fresh setup).
 */
async function setupOnFreePort(start) {
  for (let attempt = 1; ; attempt += 1) {
    const port = await freePort();
    start(port);
    const err = await listenerSettled();
    if (err === undefined) return port;
    if (err.code !== 'EADDRINUSE' || attempt === 2) throw err;
    await helper.shutdown();
    resetAndCapture();
  }
}

/** Runs `run(port)` on a free port; once more on a fresh port when the child reported EADDRINUSE. */
async function onFreePort(run) {
  const first = await run(await freePort());
  const busy = `${first.stderr ?? ''}${first.stdout ?? ''}`.includes('EADDRINUSE');
  return busy ? run(await freePort()) : first;
}

test('D-JS8 unit suffix: names and units translated as the Go and Python exporters do', () => {
  const cases = [
    [metric('parity.payload', 'By', SUM, true), 'parity_payload_bytes_total'],
    [metric('parity.items', '{item}', SUM, true), 'parity_items_total'],
    [metric('parity.wait', 'ms', SUM, true), 'parity_wait_milliseconds_total'],
    [metric('parity.busy_total', 's', SUM, true), 'parity_busy_seconds_total'],
    [metric('parity.util', '1', GAUGE), 'parity_util_ratio'],
    [metric('parity.share', '1', SUM, false), 'parity_share'],
    [metric('parity.count', '1', SUM, true), 'parity_count_total'],
    [metric('parity.lat', 's', HISTOGRAM), 'parity_lat_seconds'],
    [metric('http.server.request.duration', 's', HISTOGRAM), 'http_server_request_duration_seconds'],
    [metric('parity.conns', '{connection}', SUM, false), 'parity_conns'],
    [metric('parity.size_bytes', 'By', GAUGE), 'parity_size_bytes'],
    [metric('parity.rate', 'By/s', GAUGE), 'parity_rate_bytes_per_second'],
    [metric('parity.freq', '1/s', GAUGE), 'parity_freq_per_second'],
    [metric('parity.speed', 'm/h', GAUGE), 'parity_speed_meters_per_hour'],
    [metric('parity.odd', 'foo-bar', GAUGE), 'parity_odd_foo_bar'],
    [metric('parity.none', '', SUM, true), 'parity_none_total'],
    [metric('parity-x/y__z', '', GAUGE), 'parity_x_y_z'],
  ];
  for (const [m, want] of cases) assert.equal(prometheusName(m), want, `${m.descriptor.name} [${m.descriptor.unit}]`);
  assert.equal(unitSuffix('{req}/s', true), 'per_second');
  assert.equal(unitSuffix('By/', true), 'By');
  assert.equal(unitSuffix('__', true), '');
  assert.equal(unitSuffix('KiBy', false), 'kibibytes');
});

test('D-JS8 serializer: translated name, no # UNIT line, empty collection writes no placeholder comment', () => {
  const resource = { attributes: { 'service.name': 'svc' } };
  const empty = serializeMetrics({ resource, scopeMetrics: [] });
  assert.equal(empty, '# HELP target_info Target metadata\n# TYPE target_info gauge\ntarget_info{service_name="svc"} 1\n');
  const one = serializeMetrics({
    resource,
    scopeMetrics: [{
      scope: { name: 'parity' },
      metrics: [{ ...metric('parity.payload', 'By', SUM, true), aggregationTemporality: 1, dataPoints: [{ attributes: {}, value: 5, startTime: [0, 0], endTime: [0, 0] }] }],
    }],
  });
  assert.match(one, /\n# TYPE parity_payload_bytes_total counter\nparity_payload_bytes_total\{otel_scope_name="parity",otel_scope_schema_url="",otel_scope_version=""\} 5\n/);
  assert.doesNotMatch(one, /# UNIT/);
});

test('OTEL_HELPER_METRICS_PORT: code > env > 9464; invalid env values warn and use 9464', () => {
  const warned = [];
  const warn = (message, attrs) => warned.push(`${message} ${attrs.value}`);
  assert.equal(resolveMetricsPort(undefined, warn), 0); // the beforeEach value
  delete process.env.OTEL_HELPER_METRICS_PORT;
  assert.equal(resolveMetricsPort(undefined, warn), 9464);
  for (const [raw, want] of [[' 9100 ', 9100], ['+9100', 9100], ['+0', 0], ['0', 0], ['65535', 65535], ['09100', 9100], ['0'.repeat(40) + '9100', 9100]]) {
    process.env.OTEL_HELPER_METRICS_PORT = raw;
    assert.ok(Object.is(resolveMetricsPort(undefined, warn), want), raw);
  }
  assert.deepEqual(warned, []);
  for (const raw of ['abc', '9_464', '65536', '-1', '-0', '1.5', '1e3', '0x10', '９４６４', '1'.repeat(40), '++1']) {
    process.env.OTEL_HELPER_METRICS_PORT = raw;
    assert.equal(resolveMetricsPort(undefined, warn), 9464, raw);
  }
  assert.equal(warned.length, 11);
  assert.ok(warned.every((w) => w.startsWith('invalid port ')));
  process.env.OTEL_HELPER_METRICS_PORT = 'abc';
  assert.equal(resolveMetricsPort(9464, warn), 9464);
  assert.equal(resolveMetricsPort(0, warn), 0);
  assert.equal(warned.length, 11, 'a code value never reads the env');
});

test('prometheusMetricsPort code option: an integer in [0, 65535], else a TypeError naming it', () => {
  assert.equal(checkCodeMetricsPort(undefined), undefined);
  assert.equal(checkCodeMetricsPort(0), 0);
  for (const bad of [-1, 65536, 1.5, Number.NaN, '9464', null, true]) {
    assert.throws(() => helper.setup({ prometheusMetricsPort: bad }), /setup\(\): prometheusMetricsPort must be an integer in \[0, 65535\]/, String(bad));
  }
  process.env.OTEL_HELPER_METRICS_PORT = 'abc';
  const r = helper.setup({ prometheusMetricsPort: 0 });
  assert.equal(r.prometheusMetricsPort, 0);
  assert.ok(!records.some((x) => x.line.includes('invalid port')));
});

test('Resolved.prometheusMetricsPort reports the env value and an invalid one warns once', () => {
  process.env.OTEL_HELPER_METRICS_PORT = 'nope';
  const r = helper.setup({ metricExporters: ['none'] });
  assert.equal(r.prometheusMetricsPort, 9464);
  assert.equal(records.filter((x) => x.line === 'otelhelper: invalid port variable=OTEL_HELPER_METRICS_PORT value=nope using=9464').length, 1);
});

test('mounted handler status: 503 before setup, scrape when active, 503 after shutdown', async () => {
  const before = await mockScrape();
  assert.equal(before.status, 503);
  assert.match(before.body, /prometheus metrics reader not configured/);
  assert.equal(before.headers['content-type'], CONTENT_TYPE);
  const r = helper.setup();
  assert.deepEqual(r.metricExporters, ['prometheus'], 'no endpoint: the Prometheus fallback (ADR-007)');
  const meter = api.metrics.getMeter('parity', '1.2.3');
  meter.createCounter('parity.payload', { unit: 'By' }).add(5);
  meter.createHistogram('parity.lat', { unit: 's' }).record(0.25);
  const active = await mockScrape();
  assert.equal(active.status, 200);
  assert.equal(active.headers['content-type'], 'text/plain; version=0.0.4; charset=utf-8');
  assert.equal(active.headers['content-length'], Buffer.byteLength(active.body));
  assert.match(active.body, /^# HELP target_info Target metadata\n# TYPE target_info gauge\ntarget_info\{[^\n]*service_name="svc"/);
  assert.match(active.body, /\nparity_payload_bytes_total\{otel_scope_name="parity",otel_scope_schema_url="",otel_scope_version="1\.2\.3"\} 5\n/);
  assert.match(active.body, /\nparity_lat_seconds_count\{[^\n]*\} 1\n/);
  assert.doesNotMatch(active.body, /# UNIT|process_cpu|nodejs_/);
  const head = await mockScrape({ method: 'HEAD' });
  assert.equal(head.status, 200);
  assert.equal(head.body, undefined);
  const shutting = helper.shutdown();
  assert.equal((await mockScrape()).status, 503, 'a shutdown in progress already answers 503');
  await shutting;
  assert.equal((await mockScrape()).status, 503);
});

test('mounted handler status: Prometheus deliberately off answers 200 with an empty body', async () => {
  for (const env of [{ OTEL_METRICS_EXPORTER: 'none' }, { OTEL_SDK_DISABLED: 'true' }, { OTEL_METRICS_EXPORTER: 'console' }, { OTEL_HELPER_DISABLED_SIGNALS: 'metrics' }]) {
    Object.assign(process.env, env);
    helper.setup();
    const res = await mockScrape();
    assert.equal(res.status, 200, JSON.stringify(env));
    assert.equal(res.body, '');
    await helper.shutdown();
    resetAndCapture();
    for (const k of Object.keys(env)) delete process.env[k];
  }
});

test('mounted handler status: a foreign global MeterProvider answers 503 and starts no listener', async () => {
  const foreign = new MeterProvider();
  assert.equal(api.metrics.setGlobalMeterProvider(foreign), true);
  try {
    process.env.OTEL_HELPER_METRICS_PORT = String(await freePort());
    helper.setup();
    assert.ok(records.some((r) => r.line.includes('a global MeterProvider is already registered')));
    assert.equal((await mockScrape()).status, 503);
    await assert.rejects(get(Number(process.env.OTEL_HELPER_METRICS_PORT), '/metrics'), { code: 'ECONNREFUSED' });
  } finally {
    await helper.shutdown();
    testing.reset();
    api.metrics.disable();
    await foreign.shutdown();
  }
});

test('mounted handler status: a reader that failed to build answers 503 (reported, setup does not throw)', async () => {
  const original = endpoint.createPrometheusReader;
  endpoint.createPrometheusReader = () => {
    throw new Error('boom');
  };
  try {
    helper.setup();
  } finally {
    endpoint.createPrometheusReader = original;
  }
  assert.ok(records.some((r) => r.line.includes('the prometheus exporter could not be built; this signal is not exported to it signal=metrics error=boom')));
  assert.equal((await mockScrape()).status, 503);
});

test('mounted handler: a failed collection answers 500 while active, 503 once shut down; a sent response is only ended', async () => {
  helper.setup();
  const reader = testing.getState().runtime.active.prometheus;
  const collect = reader.collect;
  reader.collect = () => Promise.reject(new Error('collect failed'));
  try {
    const failed = await mockScrape();
    assert.equal(failed.status, 500);
    assert.equal(failed.body, 'otelhelper: metric collection failed: collect failed\n');
    reader.collect = () => {
      testing.getState().runtime.active.shutdown = Promise.resolve();
      return Promise.reject(new Error('reader shut down'));
    };
    assert.equal((await mockScrape()).status, 503);
  } finally {
    reader.collect = collect;
    testing.getState().runtime.active.shutdown = undefined;
  }
  const sent = await mockScrape(undefined, { headersSent: true });
  assert.equal(sent.body, undefined, 'headers already sent: end() only');
});

test('mounted handler: collection errors are a helper ERROR on the sink (redacted, in the parity helper_log) and the scrape still answers', async () => {
  helper.setup();
  api.metrics.getMeter('parity').createObservableGauge('parity.broken').addCallback(() => {
    throw new Error('callback failed token=s3cr3t');
  });
  api.metrics.getMeter('parity').createObservableGauge('parity.broken2').addCallback(() => {
    throw 'plain string';
  });
  const res = await mockScrape();
  assert.equal(res.status, 200);
  const errors = records.filter((r) => r.line.includes('metric collection errors'));
  assert.equal(errors.length, 1, JSON.stringify(records));
  assert.equal(errors[0].origin, 'helper');
  assert.equal(errors[0].level, 'error');
  assert.equal(
    errors[0].line,
    'otelhelper: metric collection errors; the scrape answered with the metrics that were collected count=2 error="callback failed token=<redacted>"',
  );
  assert.ok(!records.some((r) => r.line.includes('s3cr3t')), 'the secret never reaches the sink');
  assert.deepEqual(classify(errors[0]), { diagnostics: true, helperLog: true }, 'the parity driver puts it in helper_log');
});

test('mounted handler: a failed collection with a non-Error reason or a credential answers a redacted 500 body', async () => {
  helper.setup();
  const reader = testing.getState().runtime.active.prometheus;
  const collect = reader.collect;
  try {
    reader.collect = () => Promise.reject(new Error('fetch https://user:pw@collector:4318/v1?api_key=abc failed'));
    const failed = await mockScrape();
    assert.equal(failed.status, 500);
    assert.equal(failed.body, 'otelhelper: metric collection failed: fetch https://collector:4318/v1 failed\n');
    reader.collect = () => Promise.reject('plain reason');
    assert.equal((await mockScrape()).body, 'otelhelper: metric collection failed: plain reason\n');
  } finally {
    reader.collect = collect;
  }
});

test('mounted handler on a real node:http server (the mock alone could drift from ServerResponse)', async () => {
  helper.setup();
  api.metrics.getMeter('parity').createCounter('parity.requests').add(1);
  const server = http.createServer(helper.metricsHandler());
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const res = await get(server.address().port, '/anything');
    assert.equal(res.status, 200);
    assert.equal(res.headers['content-type'], CONTENT_TYPE);
    assert.match(res.body, /\nparity_requests_total\{otel_scope_name="parity",otel_scope_schema_url="",otel_scope_version=""\} 1\n/);
    const head = await get(server.address().port, '/metrics', '127.0.0.1', 'HEAD');
    assert.equal(head.status, 200);
    assert.equal(head.body, '');
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('D-JS8 listener: serves only /metrics on OTEL_HELPER_METRICS_PORT, closed by shutdown, bound again by the next setup', async () => {
  const port = await setupOnFreePort((p) => {
    process.env.OTEL_HELPER_METRICS_PORT = String(p);
    helper.setup();
  });
  api.metrics.getMeter('parity').createCounter('parity.requests').add(2);
  const ok = await get(port, '/metrics?x=1');
  assert.equal(ok.status, 200);
  assert.match(ok.body, /\nparity_requests_total\{otel_scope_name="parity",otel_scope_schema_url="",otel_scope_version=""\} 2\n/);
  assert.equal((await get(port, '/metrics/')).status, 404);
  assert.equal((await get(port, '/')).status, 404);
  const listener = testing.getState().runtime.active.listener;
  assert.equal(endpoint.isHelperServer(listener), true, 'B6 excludes it from HTTP instrumentation');
  assert.equal(endpoint.isHelperServer(http.createServer()), false);
  assert.equal(endpoint.isHelperServer(null), false);
  const address = listener.address().address;
  assert.ok(address === '::' || address === '0.0.0.0', address);
  await helper.shutdown();
  await assert.rejects(get(port, '/metrics'), { code: 'ECONNREFUSED' });
  resetAndCapture();
  helper.setup();
  assert.equal(await listenerSettled(), undefined, 'the port was released by shutdown');
  assert.equal((await get(port, '/metrics')).status, 200);
  assert.ok(!records.some((r) => r.level === 'error'), JSON.stringify(records));
});

test('D-JS8 listener: absolute-form and malformed request targets', async () => {
  const port = await setupOnFreePort((p) => helper.setup({ prometheusMetricsPort: p }));
  const raw = (line) =>
    new Promise((resolve, reject) => {
      const sock = net.connect(port, '127.0.0.1');
      let data = '';
      sock.setEncoding('utf8');
      sock.on('connect', () => sock.write(`${line}\r\nHost: x\r\nConnection: close\r\n\r\n`));
      sock.on('data', (d) => {
        data += d;
      });
      sock.on('end', () => resolve(data.split('\r\n')[0]));
      sock.on('error', reject);
    });
  assert.equal(await raw('GET http://x/metrics HTTP/1.1'), 'HTTP/1.1 200 OK');
  assert.equal(await raw('GET http://x/other HTTP/1.1'), 'HTTP/1.1 404 Not Found');
  assert.equal(await raw('GET * HTTP/1.1'), 'HTTP/1.1 404 Not Found');
});

test('D-JS14 busy port: setup does not throw, the bind error is reported at ERROR and the mounted handler keeps working', async () => {
  const blocker = net.createServer();
  await new Promise((resolve) => blocker.listen(0, resolve));
  const port = blocker.address().port;
  try {
    process.env.OTEL_HELPER_METRICS_PORT = String(port);
    helper.setup();
    assert.equal((await listenerSettled())?.code, 'EADDRINUSE');
    const errors = records.filter((r) => r.level === 'error');
    assert.equal(errors.length, 1, JSON.stringify(records));
    assert.equal(errors[0].origin, 'helper');
    assert.equal(
      errors[0].line,
      `otelhelper: the /metrics listener could not bind port ${port}; metrics are served only by a mounted metricsHandler() (if another process owns the port, mount metricsHandler() and set OTEL_HELPER_METRICS_PORT=0) variable=OTEL_HELPER_METRICS_PORT port=${port} error=EADDRINUSE`,
    );
    assert.equal((await mockScrape()).status, 200);
    await helper.shutdown(); // closing a listener that never bound is not a shutdown failure
  } finally {
    await new Promise((resolve) => blocker.close(resolve));
  }
});

test('D-JS8 listener: unref()d — a script that calls setup() and returns exits by itself', async () => {
  const started = Date.now();
  const res = await onFreePort((port) => runCjs(
    `const h = require('@staffops/otel-helper');
     h.setup({ serviceName: 'svc', environment: 'DEV' });
     setTimeout(() => {
       require('node:http').get({ host: '127.0.0.1', port: ${port}, path: '/metrics', agent: false }, (r) => {
         r.resume();
         r.on('end', () => console.log(JSON.stringify({ status: r.statusCode })));
       });
     }, 100);`,
    { env: { OTEL_HELPER_METRICS_PORT: String(port), OTEL_TRACES_EXPORTER: 'none', OTEL_LOGS_EXPORTER: 'none' } },
  ));
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(lastJson(res), { status: 200 });
  assert.equal(res.stderr, '');
  assert.ok(Date.now() - started < 20_000);
});

test('D-JS8 cluster worker with a non-zero port: warned (each scrape reaches one worker)', async () => {
  // cluster.fork() re-runs a script file (not an -e snippet): write one to a temp dir.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'otel-helper-cluster-'));
  const script = path.join(dir, 'cluster.cjs');
  fs.writeFileSync(
    script,
    `const cluster = require('node:cluster');
     if (cluster.isPrimary) {
       const w = cluster.fork();
       w.on('message', (m) => { console.log(JSON.stringify(m)); w.disconnect(); });
     } else {
       const testing = require(${JSON.stringify(require.resolve('@staffops/otel-helper/testing'))});
       const lines = [];
       testing.setDiagnosticSink((l) => lines.push(l));
       const h = require(${JSON.stringify(require.resolve('@staffops/otel-helper'))});
       h.setup({ serviceName: 'svc', environment: 'DEV' });
       // Let the primary finish binding the shared handle before the worker exits.
       setTimeout(() => process.send(lines.filter((l) => l.includes('cluster worker') || l.includes('could not bind'))), 300);
     }`,
  );
  try {
    let port;
    const res = await onFreePort((p) => {
      port = p;
      return runNode([script], { env: { OTEL_HELPER_METRICS_PORT: String(p), OTEL_TRACES_EXPORTER: 'none', OTEL_LOGS_EXPORTER: 'none' } });
    });
    assert.equal(res.status, 0, res.stderr);
    assert.deepEqual(lastJson(res), [
      `otelhelper: cluster worker serving /metrics on port ${port}: if several workers use this port, each scrape reaches one of them at random; give each worker its own port (e.g. base + a per-worker slot) or use OTLP only variable=OTEL_HELPER_METRICS_PORT`,
    ]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('Prometheus fallback rules: endpoint → otlp only; prometheus listed with an endpoint → both readers', () => {
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://collector.example.com:4318';
  process.env.OTEL_EXPORTER_OTLP_PROTOCOL = 'http/protobuf';
  assert.deepEqual(helper.setup().metricExporters, ['otlp']);
  assert.equal(testing.getState().runtime.active.prometheus, undefined);
  testing.reset();
  process.env.OTEL_METRICS_EXPORTER = 'prometheus,otlp';
  assert.deepEqual(helper.setup().metricExporters, ['prometheus', 'otlp']);
  assert.notEqual(testing.getState().runtime.active.prometheus, undefined);
});

test('A21 disabled-metric globs: case-sensitive, * any run (dots included), ? one character, every other character literal', () => {
  const cases = [
    ['parity.*', 'parity.requests', true],
    ['par*load', 'parity.payload', true],
    ['*', 'a.b/c', true],
    ['parity.r?quests', 'parity.requests', true],
    ['parity.payloa??', 'parity.payload', false],
    ['parity.[pr]*', 'parity.payload', false],
    ['parity.[pr]*', 'parity.[pr]x', true],
    ['PARITY.*', 'parity.requests', false],
    ['a*b*c', 'axxbyyc', true],
    ['a*b*c', 'axxbyy', false],
    ['**', '', true],
    ['?', '', false],
    ['a\\*', 'a\\bc', true],
    ['', '', true],
    ['', 'x', false],
  ];
  for (const [pattern, name, want] of cases) assert.equal(globMatch(pattern, name), want, `${pattern} ~ ${name}`);
});

test('A21 disabled metrics: code wins over env ([] included), items trimmed, blanks dropped; code type checked', () => {
  process.env.OTEL_HELPER_DISABLED_METRICS = ' parity.* ,, \t';
  assert.deepEqual(resolveDisabledMetrics(undefined), ['parity.*']);
  assert.deepEqual(resolveDisabledMetrics([]), []);
  assert.deepEqual(resolveDisabledMetrics([' a ', '']), ['a']);
  assert.deepEqual(helper.setup({ disabledMetrics: [' x.* '] }).disabledMetrics, ['x.*']);
  for (const bad of ['a.*', [1], null]) assert.throws(() => helper.setup({ disabledMetrics: bad }), /setup\(\): disabledMetrics must be an array of strings/);
});

test('A21 disabled metrics: dropped from the scrape', async () => {
  process.env.OTEL_HELPER_DISABLED_METRICS = 'par*load';
  helper.setup();
  const meter = api.metrics.getMeter('parity');
  meter.createCounter('parity.payload', { unit: 'By' }).add(5);
  meter.createCounter('parity.requests').add(1);
  const { body } = await mockScrape();
  assert.doesNotMatch(body, /parity_payload/);
  assert.match(body, /\nparity_requests_total\{/);
});

test('A21 dropping exporter: filters the collection, keeps the wrapped exporter preferences', async () => {
  const exported = [];
  const inner = {
    export: (rm, done) => {
      exported.push(rm);
      done({ code: 0 });
    },
    forceFlush: () => Promise.resolve('flushed'),
    shutdown: () => Promise.resolve('closed'),
    selectAggregationTemporality: function () { return this === inner ? 0 : -1; },
    selectAggregation: function () { return this === inner ? { type: 'drop' } : undefined; },
  };
  assert.equal(droppingExporter(inner, undefined), inner);
  const wrapped = droppingExporter(inner, (n) => n === 'drop.me');
  const rm = { resource: {}, scopeMetrics: [
    { scope: { name: 's1' }, metrics: [{ descriptor: { name: 'drop.me' } }] },
    { scope: { name: 's2' }, metrics: [{ descriptor: { name: 'keep' } }, { descriptor: { name: 'drop.me' } }] },
  ] };
  await new Promise((resolve) => wrapped.export(rm, resolve));
  assert.deepEqual(exported[0].scopeMetrics.map((sm) => [sm.scope.name, sm.metrics.map((m) => m.descriptor.name)]), [['s2', ['keep']]]);
  assert.equal(wrapped.selectAggregationTemporality(0), 0);
  assert.deepEqual(wrapped.selectAggregation(0), { type: 'drop' });
  assert.equal(await wrapped.forceFlush(), 'flushed');
  assert.equal(await wrapped.shutdown(), 'closed');
  const bare = droppingExporter({ export: inner.export, forceFlush: inner.forceFlush, shutdown: inner.shutdown }, () => false);
  assert.equal(bare.selectAggregationTemporality, undefined);
  assert.equal(bare.selectAggregation, undefined);
});

test('A21 disabled metrics: dropped from the console (push) exporter too', () => {
  const res = runCjs(
    `const h = require('@staffops/otel-helper');
     h.setup({ serviceName: 'svc', environment: 'DEV', exportIntervalMs: 60000 });
     const m = require('@opentelemetry/api').metrics.getMeter('parity');
     m.createCounter('parity.payload').add(5);
     m.createCounter('parity.requests').add(1);
     h.shutdown();`,
    { env: { OTEL_METRICS_EXPORTER: 'console', OTEL_HELPER_DISABLED_METRICS: 'parity.p*', OTEL_TRACES_EXPORTER: 'none', OTEL_LOGS_EXPORTER: 'none' } },
  );
  assert.equal(res.status, 0, res.stderr);
  assert.match(res.stdout, /parity\.requests/);
  assert.doesNotMatch(res.stdout, /parity\.payload/);
});

test('A21 listener: GET and HEAD /metrics only; other methods 405 with Allow', async () => {
  const port = await setupOnFreePort((p) => helper.setup({ prometheusMetricsPort: p }));
  assert.equal((await get(port, '/metrics', '127.0.0.1', 'HEAD')).status, 200);
  const post = await get(port, '/metrics', '127.0.0.1', 'POST');
  assert.equal(post.status, 405);
  assert.equal(post.headers.allow, 'GET, HEAD');
  assert.equal((await get(port, '/other', '127.0.0.1', 'POST')).status, 404);
});

test('D1 colliding label names: merged, sorted values joined with ";" (data points and target_info); data-point keys win over otel_scope_*', () => {
  const { mergedLabels } = endpoint;
  assert.deepEqual({ ...mergedLabels({ a_b: 'y', 'a.b': 'x', n: 3, 'x..y': true }) }, { a_b: 'x;y', n: '3', x_y: 'true' });
  // Values are sorted, not keys (Go exporter.go slices.Sort(vals)).
  assert.equal(mergedLabels({ 'a.b': 'y', a_b: 'x' }).a_b, 'x;y');
  assert.equal(mergedLabels({ 'a.b': 5, a_b: true, 'a-b': ['a', 'b'] }).a_b, '5;["a","b"];true');
  assert.deepEqual({ ...mergedLabels({ 'otel.scope.name': 'dp' }, { otel_scope_name: 'parity', otel_scope_version: '' }) }, { otel_scope_name: 'dp', otel_scope_version: '' });
  const text = serializeMetrics({
    resource: { attributes: { 'service.name': 'svc', 'x.y': '1', x_y: '2' } },
    scopeMetrics: [{
      scope: { name: 'parity', version: '1', schemaUrl: 'https://example.com/s' },
      metrics: [{ ...metric('parity.collide', '', SUM, true), aggregationTemporality: 1, dataPoints: [{ attributes: { 'a.b': 'x', a_b: 'y' }, value: 1, startTime: [0, 0], endTime: [0, 0] }] }],
    }],
  });
  assert.match(text, /\ntarget_info\{service_name="svc",x_y="1;2"\} 1\n/);
  assert.match(text, /\nparity_collide_total\{a_b="x;y",otel_scope_name="parity",otel_scope_schema_url="https:\/\/example\.com\/s",otel_scope_version="1"\} 1\n/);
});

test('A21 fix round: listener 404/405 carry text bodies', async () => {
  const port = await setupOnFreePort((p) => helper.setup({ prometheusMetricsPort: p }));
  const missing = await get(port, '/other');
  assert.equal(missing.body, '404 page not found\n');
  assert.equal(missing.headers['content-type'], 'text/plain; charset=utf-8');
  assert.equal((await get(port, '/metrics', '127.0.0.1', 'POST')).body, 'Method Not Allowed\n');
  assert.equal((await get(port, '/other', '127.0.0.1', 'HEAD')).body, '');
});

test('A21 fix round: a client holding a connection (partial request, keep-alive) never keeps the process alive', async () => {
  // The child sets up, then a stalled client sends half a request and a
  // keep-alive agent scrapes; the child returns from its script right after.
  const net = require('node:net');
  const { spawn } = require('node:child_process');
  const attempt = async (port) => {
    const script = `
    const h = require('@staffops/otel-helper');
    h.setup({ serviceName: 'svc', environment: 'DEV' });
    process.stdout.write('ready\\n');
    const http = require('node:http');
    const agent = new http.Agent({ keepAlive: true });
    setTimeout(() => {
      http.get({ host: '127.0.0.1', port: ${port}, path: '/metrics', agent }, (r) => {
        r.resume();
        r.on('end', () => process.stdout.write('scraped\\n'));
      });
    }, 300);`;
    const started = Date.now();
    const child = spawn(process.execPath, ['-e', script], {
      cwd: require('node:path').resolve(__dirname, '..'),
      env: { ...process.env, OTEL_HELPER_METRICS_PORT: String(port), OTEL_TRACES_EXPORTER: 'none', OTEL_LOGS_EXPORTER: 'none' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let out = '';
    let stderr = '';
    // Measured from the end of setup(): module loading and setup (which
    // builds the instrumentations) are not what this test is about.
    let readyAt;
    child.stdout.on('data', (d) => {
      out += d;
      if (readyAt === undefined && out.includes('ready')) readyAt = Date.now();
    });
    child.stderr.on('data', (d) => {
      stderr += d;
    });
    // The stalled client: connects once the listener is up and sends half a request.
    let stalled;
    const stalledClosed = new Promise((resolve) => {
      const tryConnect = () => {
        let connected = false;
        stalled = net.connect(port, '127.0.0.1');
        stalled.on('connect', () => {
          connected = true;
          stalled.write('GET /metrics HTTP/1.1\r\nHost: x\r\n');
        });
        stalled.on('error', () => {
          if (!connected) setTimeout(tryConnect, 20);
        });
        stalled.on('close', () => {
          if (connected) resolve(Date.now() - started);
        });
      };
      tryConnect();
    });
    const exitedAfter = await new Promise((resolve) => child.on('exit', (code) => resolve({ code, ms: Date.now() - started })));
    if (stderr.includes('EADDRINUSE')) stalled.destroy();
    return { stderr, out, readyAt, exitedAfter, stalledClosed, stalled: () => stalled };
  };
  const res = await onFreePort(attempt);
  assert.equal(res.exitedAfter.code, 0, res.stderr);
  const afterReady = Date.now() - res.readyAt;
  assert.ok(afterReady < 2_000, `the child exited ${afterReady} ms after setup() returned (${res.exitedAfter.ms} ms after spawn)`);
  assert.match(res.out, /scraped/);
  const closedAfter = await Promise.race([res.stalledClosed, new Promise((resolve) => setTimeout(() => resolve(Infinity), 6_500))]);
  res.stalled().destroy();
  assert.ok(closedAfter < 6_500, `the stalled client was closed after ${closedAfter} ms`);
});

test('errorText never throws: a throwing toString or message getter, a revoked Proxy, a non-string message (B14 round 3)', () => {
  const { errorText, UNPRINTABLE_ERROR } = require('../dist/internal/metrics-endpoint.js');
  assert.equal(errorText(new Error('boom')), 'boom');
  assert.equal(errorText('plain'), 'plain');
  assert.equal(errorText({ toString() { throw new Error('x'); } }), UNPRINTABLE_ERROR);
  const getter = new Error('x');
  Object.defineProperty(getter, 'message', { get() { throw new Error('y'); } });
  assert.equal(errorText(getter), UNPRINTABLE_ERROR);
  const { proxy, revoke } = Proxy.revocable({}, {});
  revoke();
  assert.equal(errorText(proxy), UNPRINTABLE_ERROR);
  assert.equal(errorText(Object.assign(new Error('x'), { message: 42 })), '42');
  assert.equal(errorText(Object.assign(new Error('x'), { message: { a: 1 } })), UNPRINTABLE_ERROR);
  assert.equal(errorText(Symbol('s')), 'Symbol(s)');
});
