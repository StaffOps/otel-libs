'use strict';
// Flush on a natural process end (task B12 fix round, dev H1, 2026-11-03;
// design D-JS10, spec JS7): the first successful setup() of the main thread
// registers one `process.once('beforeExit')` listener that calls shutdown()
// when the app did not. Python's SDK does the same at interpreter exit
// (atexit); Go needs a deferred Shutdown (D-JS14). Every case runs in a child
// process: the listener and the exit are process state.
const test = require('node:test');
const assert = require('node:assert/strict');
const { runCjs, lastJson } = require('./helpers.cjs');

// The console exporter behind the batch processor, with a schedule delay far
// beyond the run: a span reaches stdout only through a flush.
const ENV = {
  OTEL_SERVICE_NAME: 'svc',
  OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=DEV',
  OTEL_TRACES_EXPORTER: 'console',
  OTEL_METRICS_EXPORTER: 'none',
  OTEL_LOGS_EXPORTER: 'none',
  OTEL_BSP_SCHEDULE_DELAY: '60000',
};
const SPAN = /name: 'exit-probe-span'/g;

test('JS7 D-JS10 a process that ends on its own exports its spans (beforeExit flush)', () => {
  const res = runCjs(`
    const h = require('@staffops/otel-helper');
    h.setup();
    h.getTracer('exit-test').startSpan('exit-probe-span').end();
  `, { env: ENV });
  assert.equal(res.status, 0, res.stderr);
  assert.equal((res.stdout.match(SPAN) ?? []).length, 1, res.stdout);
});

test('JS7 D-JS10 without the listener the same span is lost (what the listener fixes)', () => {
  const res = runCjs(`
    const h = require('@staffops/otel-helper');
    h.setup();
    process.removeAllListeners('beforeExit');
    h.getTracer('exit-test').startSpan('exit-probe-span').end();
  `, { env: ENV });
  assert.equal(res.status, 0, res.stderr);
  assert.doesNotMatch(res.stdout, SPAN);
});

test('D-JS10 the listener does not loop and does not repeat an app shutdown()', () => {
  const res = runCjs(`
    const h = require('@staffops/otel-helper');
    h.setup();
    let emitted = 0;
    process.on('beforeExit', () => { emitted += 1; if (emitted === 3) console.log(JSON.stringify({ emitted, left: process.listenerCount('beforeExit') - 1 })); });
    h.getTracer('exit-test').startSpan('exit-probe-span').end();
    h.shutdown().then(() => { h.getTracer('exit-test').startSpan('after-shutdown').end(); });
    process.on('exit', () => console.log(JSON.stringify({ exitEmitted: emitted, left: process.listenerCount('beforeExit') - 1 })));
  `, { env: ENV });
  assert.equal(res.status, 0, res.stderr);
  // The app's own shutdown() flushed the span once; the helper's listener ran
  // once, did nothing, and is gone (the app's counter is the only listener left).
  assert.equal((res.stdout.match(SPAN) ?? []).length, 1, res.stdout);
  const last = lastJson(res);
  assert.equal(last.left, 0);
  assert.ok(last.exitEmitted >= 1 && last.exitEmitted <= 2, `beforeExit emitted ${String(last.exitEmitted)} times`);
});

test('D-JS10 re-setup does not stack listeners; a setup after the listener fired registers one again', () => {
  const res = runCjs(`
    const h = require('@staffops/otel-helper');
    const n = () => process.listenerCount('beforeExit');
    const counts = [n()];
    h.setup(); counts.push(n());
    h.setup(); counts.push(n());
    h.shutdown().then(() => {
      h.setup(); counts.push(n());
      process.emit('beforeExit', 0); counts.push(n());
      h.shutdown().then(() => { h.setup(); counts.push(n()); console.log(JSON.stringify(counts)); return h.shutdown(); });
    });
  `, { env: ENV });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(lastJson(res), [0, 1, 1, 1, 0, 1]);
});

test('D-JS10 a failed setup registers no listener; /testing reset() removes it', () => {
  const res = runCjs(`
    require('./dist/internal/setup.js').setupFault.at = (s) => { if (s === 'providers') throw new Error('injected'); };
    const h = require('@staffops/otel-helper');
    const out = {};
    try { h.setup(); } catch (e) { out.threw = e.message; }
    out.afterFailure = process.listenerCount('beforeExit');
    require('./dist/internal/setup.js').setupFault.at = undefined;
    h.setup();
    out.afterSetup = process.listenerCount('beforeExit');
    require('@staffops/otel-helper/testing').reset();
    out.afterReset = process.listenerCount('beforeExit');
    console.log(JSON.stringify(out));
  `, { env: ENV });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(lastJson(res), { threw: 'injected', afterFailure: 0, afterSetup: 1, afterReset: 0 });
});

test('D-JS10 a worker thread setup() registers no listener (the main thread owns the exit)', () => {
  const res = runCjs(`
    const { Worker } = require('node:worker_threads');
    const w = new Worker(\`
      const { parentPort } = require('node:worker_threads');
      const h = require('@staffops/otel-helper');
      h.setup({ prometheusMetricsPort: 0 });
      parentPort.postMessage(process.listenerCount('beforeExit'));
      h.shutdown();
    \`, { eval: true });
    w.once('message', (count) => console.log(JSON.stringify({ worker: count, main: process.listenerCount('beforeExit') })));
  `, { env: ENV });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(lastJson(res), { worker: 0, main: 0 });
});

test('D-JS10 a failing shutdown at exit is reported on the sink, never thrown', () => {
  const res = runCjs(`
    const h = require('@staffops/otel-helper');
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
    h.setup({ spanProcessors: [{ onStart() {}, onEnd() {}, forceFlush: async () => {}, shutdown: async () => { throw new Error('boom'); } }] });
    process.on('exit', () => console.log(JSON.stringify(lines.filter((l) => l.includes('beforeExit')))));
  `, { env: ENV });
  assert.equal(res.status, 0, res.stderr);
  const [line] = lastJson(res);
  assert.match(line, /^otelhelper: shutdown\(\) at process exit \(beforeExit\) failed; some telemetry was not exported error=/);
});
