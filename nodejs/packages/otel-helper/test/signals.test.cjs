'use strict';
// Signal selection (task B4c): spec JS2 (OTEL_CONFIG_FILE), contract R2, R8
// and the design D1 rows "Signal on/off" and "Metrics exporters / interval /
// exemplars"; Node design D-JS3 rows OTEL_{TRACES,METRICS,LOGS}_EXPORTER,
// OTEL_SDK_DISABLED, OTEL_METRIC_EXPORT_INTERVAL; D-JS14 (reader timeout cap,
// timer bound of the interval). The wire side is asserted by the parity
// fixture (export.*_exporter_* and export.sdk_disabled cases).
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('@opentelemetry/api');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { checkCodeSignals, parseExporterEnv, resolveSignals, MAX_EXPORT_INTERVAL_MS } = require('../dist/internal/signals.js');
const { metricTimeout, tryBuild } = require('../dist/internal/setup.js');
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

const warnings = () => records.filter((r) => r.level === 'warn').map((r) => r.line);

/** A collecting Warn. */
function collector() {
  const seen = [];
  const warn = (message, attrs) => seen.push(`${message} ${JSON.stringify(attrs)}`);
  return { seen, warn };
}

const NO_OTLP = Object.freeze({ traces: undefined, metrics: undefined, logs: undefined });
const OTLP_ALL = Object.freeze({ traces: {}, metrics: {}, logs: {} });

test('R8 exporter list grammar: trimmed, case-insensitive, deduplicated; logging = console (warned); unknown warned and dropped', () => {
  const cases = [
    // [value, signal, want, warning substrings]
    [' OTLP , console,otlp ', 'traces', ['otlp', 'console'], []],
    ['Logging', 'logs', ['console'], ['deprecated exporter value "logging"']],
    ['bogus,console', 'traces', ['console'], ['unknown exporter; ignored', '"value":"bogus"']],
    ['prometheus', 'traces', undefined, ['unknown exporter; ignored']],
    ['prometheus,otlp', 'metrics', ['prometheus', 'otlp'], []],
    ['bogus', 'metrics', undefined, ['unknown exporter']],
    [' , ,', 'logs', undefined, []],
    [' \t', 'logs', undefined, []],
    ['none', 'metrics', [], []],
    ['NONE,otlp', 'traces', [], ['"none" combined with other exporters']],
    ['none,bogus', 'logs', [], ['unknown exporter']],
  ];
  for (const [value, signal, want, subs] of cases) {
    process.env[`OTEL_${signal.toUpperCase()}_EXPORTER`] = value;
    const { seen, warn } = collector();
    assert.deepEqual(parseExporterEnv(signal, warn), want, JSON.stringify(value));
    for (const sub of subs) assert.ok(seen.some((s) => s.includes(sub)), `${JSON.stringify(value)}: ${seen.join(' | ')}`);
    if (subs.length === 0) assert.deepEqual(seen, [], JSON.stringify(value));
    delete process.env[`OTEL_${signal.toUpperCase()}_EXPORTER`];
  }
});

test('R8 code options fail fast: metricExporters, exportIntervalMs, disabledSignals', () => {
  assert.deepEqual(checkCodeSignals({ metricExporters: [' OTLP', 'console', 'otlp', ' '] }).metricExporters, ['otlp', 'console']);
  assert.deepEqual(checkCodeSignals({ metricExporters: [] }).metricExporters, []);
  assert.deepEqual(checkCodeSignals({ metricExporters: ['none'] }).metricExporters, []);
  assert.ok(Object.isFrozen(checkCodeSignals({ metricExporters: ['otlp'] }).metricExporters));
  assert.deepEqual(checkCodeSignals({}), {});
  const bad = [
    [{ metricExporters: 'otlp' }, /metricExporters must be an array of strings, got string/],
    [{ metricExporters: ['otlp', 1] }, /an item of type number/],
    [{ metricExporters: ['bogus'] }, /unknown metric exporter "bogus"; valid values: otlp, prometheus, console, none/],
    [{ metricExporters: ['logging'] }, /unknown metric exporter "logging"/],
    [{ metricExporters: ['none', 'otlp'] }, /"none" cannot be combined/],
    [{ exportIntervalMs: 0 }, /exportIntervalMs must be an integer number of milliseconds in \[1, 2147483647\], got 0/],
    [{ exportIntervalMs: 1.5 }, /got 1\.5/],
    [{ exportIntervalMs: MAX_EXPORT_INTERVAL_MS + 1 }, /exportIntervalMs/],
    [{ exportIntervalMs: '5000' }, /got string/],
    [{ exportIntervalMs: Number.NaN }, /got NaN/],
    [{ disabledSignals: ['traces', 'profiles'] }, /disabledSignals has unknown signal "profiles"; valid values: traces, metrics, logs/],
    [{ disabledSignals: null }, /disabledSignals must be an array of strings, got null/],
  ];
  for (const [options, re] of bad) assert.throws(() => checkCodeSignals(options), (err) => err instanceof TypeError && re.test(err.message), JSON.stringify(options));
  assert.deepEqual(checkCodeSignals({ disabledSignals: [' Logs', 'logs', 'traces'] }).disabledSignals, ['logs', 'traces']);
  assert.equal(checkCodeSignals({ exportIntervalMs: MAX_EXPORT_INTERVAL_MS }).exportIntervalMs, MAX_EXPORT_INTERVAL_MS);
  // Through setup(): a TypeError naming the option, nothing installed.
  assert.throws(() => helper.setup({ metricExporters: ['bogus'] }), /setup\(\): metricExporters/);
  assert.throws(() => helper.setup({ exportIntervalMs: -1 }), /setup\(\): exportIntervalMs/);
});

test('R2 metric exporters: endpoint-based default; env otlp without endpoint warns, code otlp without endpoint throws before installing', () => {
  assert.deepEqual(helper.setup().metricExporters, ['prometheus']);
  testing.reset();
  resetAndCapture();
  process.env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT = 'http://collector:4317';
  assert.deepEqual(helper.setup().metricExporters, ['otlp']);
  testing.reset();
  resetAndCapture();
  delete process.env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT;
  process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = 'http://collector:4317';
  process.env.OTEL_METRICS_EXPORTER = 'otlp,console';
  assert.deepEqual(helper.setup().metricExporters, ['console']);
  assert.equal(warnings().filter((l) => l.includes('"otlp" requires an OTLP endpoint')).length, 1);
  testing.reset();
  resetAndCapture();
  process.env.OTEL_METRICS_EXPORTER = 'otlp';
  assert.deepEqual(helper.setup().metricExporters, ['prometheus']);
  testing.reset();
  resetAndCapture();
  delete process.env.OTEL_METRICS_EXPORTER;
  assert.throws(() => helper.setup({ metricExporters: ['otlp'] }), (err) => err instanceof TypeError && /requires an endpoint/.test(err.message));
  assert.equal(api.trace.getTracerProvider().getDelegate().constructor.name, 'NoopTracerProvider');
  assert.equal(testing.getResource(), undefined);
  // Code wins over the env list and the default.
  process.env.OTEL_METRICS_EXPORTER = 'none';
  assert.deepEqual(helper.setup({ metricExporters: ['console'] }).metricExporters, ['console']);
});

test('Resolved exporter lists: default otlp for traces and logs, frozen; none disables', () => {
  const resolved = helper.setup();
  assert.deepEqual(resolved.tracesExporters, ['otlp']);
  assert.deepEqual(resolved.logsExporters, ['otlp']);
  assert.equal(resolved.sdkDisabled, false);
  for (const list of [resolved.tracesExporters, resolved.metricExporters, resolved.logsExporters]) assert.ok(Object.isFrozen(list));
  testing.reset();
  resetAndCapture();
  process.env.OTEL_TRACES_EXPORTER = 'none';
  process.env.OTEL_LOGS_EXPORTER = 'console';
  const r2 = helper.setup();
  assert.deepEqual(r2.tracesExporters, []);
  assert.deepEqual(r2.logsExporters, ['console']);
});

test('D1 "Signal on/off": OTEL_HELPER_DISABLED_SIGNALS maps to none; the standard variable and code disabledSignals win', () => {
  process.env.OTEL_HELPER_DISABLED_SIGNALS = 'traces, Metrics,profiles';
  process.env.OTEL_TRACES_EXPORTER = 'console';
  const resolved = helper.setup();
  assert.deepEqual(resolved.tracesExporters, ['console']); // the standard variable wins
  assert.deepEqual(resolved.metricExporters, []);
  assert.deepEqual(resolved.logsExporters, ['otlp']);
  assert.ok(warnings().some((l) => l.includes('unknown signal; ignored') && l.includes('value=profiles')));
  assert.ok(warnings().some((l) => l.includes('OTEL_HELPER_DISABLED_SIGNALS is deprecated')));
  testing.reset();
  resetAndCapture();
  const r2 = helper.setup({ disabledSignals: [] });
  assert.deepEqual(r2.metricExporters, ['prometheus']);
  testing.reset();
  resetAndCapture();
  assert.deepEqual(helper.setup({ disabledSignals: ['logs'] }).logsExporters, []);
});

test('D1 OTEL_SDK_DISABLED=true: no providers, no exporters, no resource; the context manager is installed; shutdown resolves', async () => {
  process.env.OTEL_SDK_DISABLED = 'TRUE';
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://collector:4317';
  const resolved = helper.setup();
  assert.equal(resolved.sdkDisabled, true);
  assert.deepEqual([resolved.tracesExporters, resolved.metricExporters, resolved.logsExporters], [[], [], []]);
  assert.equal(resolved.serviceName, 'svc'); // identity is still resolved
  assert.equal(testing.getResource(), undefined);
  assert.equal(api.trace.getTracerProvider().getDelegate().constructor.name, 'NoopTracerProvider');
  assert.equal(api.metrics.getMeterProvider().constructor.name, 'NoopMeterProvider');
  // In-process context still propagates (AsyncLocalStorage context manager).
  const key = api.createContextKey('k');
  const seen = await api.context.with(api.context.active().setValue(key, 'v'), async () => {
    await new Promise((r) => setImmediate(r));
    return api.context.active().getValue(key);
  });
  assert.equal(seen, 'v');
  await helper.shutdown();
  // Re-setup after shutdown with the SDK enabled builds the providers again.
  delete process.env.OTEL_SDK_DISABLED;
  helper.setup();
  assert.equal(api.trace.getTracerProvider().getDelegate().constructor.name, 'TracerProvider');
});

test('R8 OTEL_SDK_DISABLED unparseable: warns and reads as false (enabled)', () => {
  process.env.OTEL_SDK_DISABLED = 'maybe';
  assert.equal(helper.setup().sdkDisabled, false);
  assert.ok(warnings().some((l) => l.includes('unparseable boolean') && l.includes('variable=OTEL_SDK_DISABLED') && l.includes('value=maybe')));
  assert.notEqual(testing.getResource(), undefined);
});

test('D1 OTEL_METRIC_EXPORT_INTERVAL: positive integer ms, default 30000; garbage warns; above the timer limit clamped; code wins', () => {
  const cases = [
    [undefined, 30000, false],
    ['5000', 5000, false],
    [' +5000 ', 5000, false],
    ['007', 7, false],
    ['0', 30000, true],
    ['-1', 30000, true],
    ['5000.0', 30000, true],
    ['5e3', 30000, true],
    ['0x10', 30000, true],
    ['5_000', 30000, true],
    ['abc', 30000, true],
  ];
  for (const [value, want, warns] of cases) {
    resetAndCapture();
    if (value === undefined) delete process.env.OTEL_METRIC_EXPORT_INTERVAL;
    else process.env.OTEL_METRIC_EXPORT_INTERVAL = value;
    const { seen, warn } = collector();
    const sel = resolveSignals({}, NO_OTLP, warn);
    assert.equal(sel.metricExportIntervalMs, want, JSON.stringify(value));
    assert.equal(seen.some((s) => s.includes('invalid metric export interval')), warns, `${JSON.stringify(value)}: ${seen.join(' | ')}`);
  }
  process.env.OTEL_METRIC_EXPORT_INTERVAL = '99999999999999999999';
  const { seen, warn } = collector();
  const clamped = resolveSignals({}, NO_OTLP, warn);
  assert.equal(clamped.metricExportIntervalMs, MAX_EXPORT_INTERVAL_MS);
  assert.equal(clamped.intervalSource, 'OTEL_METRIC_EXPORT_INTERVAL');
  assert.ok(seen.some((s) => s.includes('above 2147483647 ms; clamped')));
  const code = resolveSignals({ exportIntervalMs: 1234 }, OTLP_ALL, () => assert.fail('no warning expected'));
  assert.deepEqual([code.metricExportIntervalMs, code.intervalSource], [1234, 'exportIntervalMs']);
  assert.equal(helper.setup({ exportIntervalMs: 2000 }).metricExportIntervalMs, 2000);
});

/** The metric readers the helper installed (SDK internals, test only). */
function readers() {
  return api.metrics.getMeterProvider()._sharedState.metricCollectors.map((c) => c._metricReader);
}

test('D-JS14 reader timeout: the OTLP timeout capped at the interval; the warning names the source of the cap', () => {
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://collector:4317';
  process.env.OTEL_METRIC_EXPORT_INTERVAL = '5000';
  helper.setup();
  const [reader] = readers();
  assert.equal(reader._exportInterval, 5000);
  assert.equal(reader._exportTimeout, 5000);
  const capped = warnings().filter((l) => l.includes('capped at the interval'));
  assert.equal(capped.length, 1, warnings().join('\n'));
  assert.match(capped[0], /variable=OTEL_METRIC_EXPORT_INTERVAL value=5000 timeout=10000 using=5000/);
  // A set timeout above the interval: the timeout's source, and the interval's.
  testing.reset();
  resetAndCapture();
  helper.setup({ exportTimeoutMs: 8000, exportIntervalMs: 6000 });
  assert.equal(readers()[0]._exportTimeout, 6000);
  assert.match(warnings().find((l) => l.includes('capped')), /variable=exportTimeoutMs value=8000 using=6000 interval=exportIntervalMs/);
  // Below the interval: no cap, no warning.
  const { seen, warn } = collector();
  assert.equal(metricTimeout({ timeoutMs: 100, timeoutSource: 'default' }, 30000, 'default', warn), 100);
  assert.deepEqual(seen, []);
});

test('R8 console exporters: the SDK console exporters, one pipeline per list entry, OTLP only with an endpoint', () => {
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://collector:4317';
  process.env.OTEL_METRICS_EXPORTER = 'console,otlp';
  process.env.OTEL_METRIC_EXPORT_INTERVAL = '20000';
  helper.setup();
  const names = readers().map((r) => [r._exporter.constructor.name, r._exportInterval, r._exportTimeout]);
  assert.deepEqual(names, [['OTLPMetricExporter', 20000, 10000], ['ConsoleMetricExporter', 20000, 20000]]);
});

test('R8 console exporters print to stdout: span, metric and log record (child process)', () => {
  const res = runCjs(`
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
    const api = require('@opentelemetry/api');
    const h = require('@staffops/otel-helper');
    const r = h.setup({ serviceName: 's', environment: 'DEV', metricExporters: ['console'] });
    api.trace.getTracer('t').startSpan('console-span').end();
    api.metrics.getMeter('m').createCounter('console.counter').add(1);
    api.logs.getLogger('l').emit({ severityNumber: 13, body: 'console-log' });
    h.shutdown().then(() => console.log(JSON.stringify({ t: r.tracesExporters, l: r.logsExporters })));
  `, { env: { OTEL_TRACES_EXPORTER: 'console', OTEL_LOGS_EXPORTER: 'logging', OTEL_EXPORTER_OTLP_ENDPOINT: '' } });
  assert.deepEqual(lastJson(res), { t: ['console'], l: ['console'] });
  assert.match(res.stdout, /console-span/);
  assert.match(res.stdout, /console\.counter/);
  assert.match(res.stdout, /console-log/);
});

test('tryBuild names a non-OTLP exporter in its report', () => {
  const pending = [];
  assert.equal(tryBuild('logs', () => { throw new Error('x'); }, () => 'never', pending, 'console'), undefined);
  assert.equal(pending[0][0], 'the console exporter could not be built; this signal is not exported to it');
});

test('JS2 OTEL_CONFIG_FILE: warned once per process, naming the variable only', () => {
  process.env.OTEL_CONFIG_FILE = '/etc/secret-path/otel.yaml';
  helper.setup();
  const hits = records.filter((r) => r.line.includes('OTEL_CONFIG_FILE'));
  assert.equal(hits.length, 1);
  assert.match(hits[0].line, /OTEL_CONFIG_FILE is not supported by the Node.js helper and is ignored/);
  assert.doesNotMatch(hits[0].line, /secret-path/);
  // Once per process: a second setup in the same process does not repeat it.
  const res = runCjs(`
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
    const h = require('@staffops/otel-helper');
    h.setup({ serviceName: 's', environment: 'DEV' });
    h.shutdown().then(() => { h.setup({ serviceName: 's', environment: 'DEV' }); console.log(JSON.stringify(lines.filter((l) => l.includes('OTEL_CONFIG_FILE')).length)); });
  `);
  assert.equal(lastJson(res), 1);
});

test('A19 standard variable counts as set only with a valid entry; code metricExporters wins for metrics', () => {
  process.env.OTEL_HELPER_DISABLED_SIGNALS = 'traces,metrics,logs';
  process.env.OTEL_TRACES_EXPORTER = 'bogus';
  process.env.OTEL_LOGS_EXPORTER = ' , ,';
  process.env.OTEL_METRICS_EXPORTER = 'none';
  const resolved = helper.setup();
  assert.deepEqual([resolved.tracesExporters, resolved.logsExporters], [[], []]);
  assert.ok(warnings().some((l) => l.includes('unknown exporter; ignored') && l.includes('value=bogus')));
  testing.reset();
  resetAndCapture();
  delete process.env.OTEL_METRICS_EXPORTER;
  assert.deepEqual(helper.setup({ metricExporters: ['console'] }).metricExporters, ['console']);
});

test('A19 unknown OTEL_HELPER_DISABLED_SIGNALS entries warn once per setup', () => {
  process.env.OTEL_HELPER_DISABLED_SIGNALS = 'profiles,profiles,PROFILES';
  helper.setup();
  assert.equal(warnings().filter((l) => l.includes('value=profiles')).length, 1);
});

test('B7 pending: a resolved prometheus exporter is reported once (nothing is built yet)', async () => {
  helper.setup();
  await helper.shutdown();
  helper.setup();
  assert.equal(records.filter((r) => r.level === 'info' && r.line.includes('prometheus metric exporter not implemented yet in Node; metrics are not exposed on /metrics signal=metrics')).length, 1);
});

test('A19 OTEL_SDK_DISABLED: one INFO line on the sink', () => {
  process.env.OTEL_SDK_DISABLED = 'true';
  helper.setup();
  const info = records.filter((r) => r.level === 'info' && r.line.includes('OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed'));
  assert.equal(info.length, 1);
});

test('D-JS14 a capped reader timeout below 1000 ms is warned', () => {
  const { seen, warn } = collector();
  assert.equal(metricTimeout({ timeoutMs: 10000, timeoutSource: 'default' }, 500, 'exportIntervalMs', warn), 500);
  assert.ok(seen.some((s) => s.includes('below 1000 ms')));
});

test('A19 OTEL_EXPERIMENTAL_CONFIG_FILE: warned like OTEL_CONFIG_FILE, naming the variable only', () => {
  process.env.OTEL_EXPERIMENTAL_CONFIG_FILE = '/etc/secret-path/otel.yaml';
  helper.setup();
  const hits = records.filter((r) => r.line.includes('OTEL_EXPERIMENTAL_CONFIG_FILE is not supported'));
  assert.equal(hits.length, 1);
  assert.doesNotMatch(hits[0].line, /secret-path/);
});
