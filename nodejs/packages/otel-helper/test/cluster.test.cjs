'use strict';
// The prefork model (task B9b): Node design D-JS13 and the D-JS14 cluster rows,
// spec JS9, contract design D4 (service.instance.id per process; an explicit
// id in a multi-worker server is kept and warned) and the D1 row "Explicit
// instance id in a worker" (A24). The parity fixture covers the worker ids
// (fork.* cases); these tests add what the fixture cannot see: the primary's
// own id, the code-option branch of the warning, a child_process.fork() child,
// and the /metrics listener under cluster (shared port, a primary that also
// set up, per-worker slot ports, a worker restarted on its slot), and the
// flush of a worker the primary disconnects (beforeExit, or a disconnect
// handler when a handle keeps it alive; B12, 2026-11-03).
//
// No fixed sleeps: the primary waits for real events (`cluster` 'listening',
// worker reports, a polled "could not bind" line) under one 20 s deadline that
// kills every worker and fails the run.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { runNode, lastJson } = require('./helpers.cjs');

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const BASE_ENV = { OTEL_SERVICE_NAME: 'svc', OTEL_TRACES_EXPORTER: 'none', OTEL_LOGS_EXPORTER: 'none', OTEL_RESOURCE_ATTRIBUTES: '' };

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

/** `n` distinct free ports, each chosen independently. */
async function freePorts(n) {
  const ports = new Set();
  while (ports.size < n) ports.add(await freePort());
  return [...ports];
}

// cluster.fork() re-runs a script file (not an -e snippet). Controlled by env:
// PRIMARY_SETUP=1   the primary calls setup() too (as `--require …/register` in NODE_OPTIONS would)
// SLOT_PORTS        JSON array, one entry per worker slot: a number is passed as the slot's
//                   OTEL_HELPER_METRICS_PORT in cluster.fork(env), null inherits the primary's env
// WAIT              'listening' (the primary waits for each worker's 'listening' event) or
//                   'bind-error' (each worker waits for its "could not bind" line)
// CRASH_SLOT        that slot's first worker exits(1) once ready; the primary re-forks the slot
// SCRAPE_PORTS      JSON array of ports the primary scrapes 6 times each, a new connection per scrape
// WORKER_OPTIONS    JSON setup() options of the workers
// CHILD_FORK=1      each worker runs a child_process.fork() child that calls setup() too
const SCRIPT = (testingPath, helperPath, apiPath) => `'use strict';
const cluster = require('node:cluster');
const http = require('node:http');
const testing = require(${JSON.stringify(testingPath)});
const lines = [];
testing.setDiagnosticSink((l, meta) => { if (meta && meta.origin === 'helper') lines.push(l); });
const h = require(${JSON.stringify(helperPath)});
const idOf = () => { const r = testing.getResource(); return r ? r.attributes['service.instance.id'] : null; };

if (process.env.CHILD === '1') {
  h.setup();
  process.send({ isWorker: cluster.isWorker, id: idOf(), lines }, () => process.exit(0));
} else if (cluster.isPrimary) {
  // Round-robin regardless of NODE_CLUSTER_SCHED_POLICY or the platform default (Windows: SCHED_NONE).
  cluster.schedulingPolicy = cluster.SCHED_RR;
  const fail = (msg) => {
    console.log(JSON.stringify({ error: msg }));
    for (const id in cluster.workers) cluster.workers[id].process.kill('SIGKILL');
    process.exit(1);
  };
  setTimeout(() => fail('cluster test deadline (20 s) exceeded'), 20_000).unref();
  if (process.env.PRIMARY_SETUP === '1') h.setup();
  const primary = { id: idOf(), lines: lines.slice() };
  const slotPorts = JSON.parse(process.env.SLOT_PORTS || '[null,null]');
  const crashSlot = process.env.CRASH_SLOT === undefined ? -1 : Number(process.env.CRASH_SLOT);
  const waitListening = process.env.WAIT === 'listening';
  const final = new Array(slotPorts.length);
  const crashed = [];
  let finishing = false;
  function forkSlot(slot, crash) {
    const env = { SLOT: String(slot) };
    if (slotPorts[slot] !== null) env.OTEL_HELPER_METRICS_PORT = String(slotPorts[slot]);
    const w = cluster.fork(env);
    Object.assign(w, { slot, crash, report: undefined, listened: false });
    w.on('message', (m) => { w.report = m; ready(w); });
  }
  cluster.on('listening', (w) => { w.listened = true; ready(w); });
  cluster.on('exit', (w, code) => {
    if (finishing) return;
    if (w.crash && w.report !== undefined) return forkSlot(w.slot, false);
    fail('cluster worker for slot ' + w.slot + ' exited (code ' + code + ') before the run finished');
  });
  function ready(w) {
    if (w.report === undefined || (waitListening && !w.listened) || w.counted) return;
    w.counted = true;
    if (w.crash) {
      crashed.push(w.report);
      w.send('crash');
      return;
    }
    final[w.slot] = w.report;
    if (final.filter(Boolean).length === final.length) finish().catch((err) => fail(String(err && err.stack || err)));
  }
  function get(port) {
    return new Promise((resolve) => {
      // agent: false = a new connection per scrape, so the primary's round-robin sees each one.
      http.get({ host: '127.0.0.1', port, path: '/metrics', agent: false }, (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (c) => { body += c; });
        res.on('end', () => resolve({ status: res.statusCode, body }));
      }).on('error', (err) => resolve({ status: 0, body: String(err.code || err) }));
    });
  }
  async function finish() {
    const scrapes = {};
    for (const port of JSON.parse(process.env.SCRAPE_PORTS || '[]')) {
      scrapes[port] = [];
      for (let i = 0; i < 6; i++) scrapes[port].push(await get(port));
    }
    finishing = true;
    console.log(JSON.stringify({ primary, workers: final, crashed, scrapes }));
    let left = Object.keys(cluster.workers).length;
    if (left === 0) process.exit(0);
    cluster.on('exit', () => { if (--left === 0) process.exit(0); });
    for (const id in cluster.workers) cluster.workers[id].send('done');
  }
  for (let slot = 0; slot < slotPorts.length; slot++) forkSlot(slot, slot === crashSlot);
} else {
  const slot = process.env.SLOT;
  const opts = JSON.parse(process.env.WORKER_OPTIONS || '{}');
  h.setup(opts);
  require(${JSON.stringify(apiPath)}).metrics.getMeter('cluster-test').createCounter('cluster_probe').add(1, { slot, pid: String(process.pid) });
  process.on('message', (m) => process.exit(m === 'crash' ? 1 : 0));
  const report = (child) => process.send({ slot: Number(slot), pid: process.pid, id: idOf(), lines, child });
  if (process.env.CHILD_FORK === '1') {
    const c = require('node:child_process').fork(__filename, [], { env: { ...process.env, CHILD: '1' } });
    c.on('message', (m) => report(m));
  } else if (process.env.WAIT === 'bind-error') {
    const poll = setInterval(() => {
      if (lines.some((l) => l.includes('could not bind'))) { clearInterval(poll); report(); }
    }, 10);
  } else {
    report();
  }
}
`;

function runCluster(env) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'otel-helper-cluster-'));
  const script = path.join(dir, 'cluster.cjs');
  fs.writeFileSync(script, SCRIPT(require.resolve('@staffops/otel-helper/testing'), require.resolve('@staffops/otel-helper'), require.resolve('@opentelemetry/api')));
  try {
    const res = runNode([script], { env: { ...BASE_ENV, ...env } });
    assert.equal(res.status, 0, `${res.stdout}\n${res.stderr}`);
    return lastJson(res);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

const ID_WARNING = /^otelhelper: service\.instance\.id is set explicitly in a cluster worker: unless each worker sets a different value, every worker reports the same id; leave it unset so each process gets its own key=service\.instance\.id$/;
const PORT_WARNING = (port) => `otelhelper: cluster worker serving /metrics on port ${port}: if several workers use this port, each scrape reaches one of them at random; give each worker its own port (e.g. base + a per-worker slot) or use OTLP only variable=OTEL_HELPER_METRICS_PORT`;
const slotsIn = (body) => [...body.matchAll(/cluster_probe_total\{[^}]*slot="(\d+)"/g)].map((m) => Number(m[1]));
const pidsIn = (body) => [...body.matchAll(/cluster_probe_total\{[^}]*pid="(\d+)"/g)].map((m) => Number(m[1]));

test('JS9 D-JS13 each cluster worker generates its own service.instance.id, distinct from the primary', () => {
  const out = runCluster({ PRIMARY_SETUP: '1', OTEL_HELPER_METRICS_PORT: '0' });
  const ids = [out.primary.id, ...out.workers.map((w) => w.id)];
  for (const id of ids) assert.match(id, UUID);
  assert.equal(new Set(ids).size, 3, `ids: ${ids.join(', ')}`);
  for (const lines of [out.primary.lines, ...out.workers.map((w) => w.lines)]) {
    assert.deepEqual(lines.filter((l) => l.includes('service.instance.id')), []);
  }
});

test('D4 A24 an env-provided id is kept in every worker and warned once per worker; the primary is not warned', () => {
  const out = runCluster({ PRIMARY_SETUP: '1', OTEL_HELPER_METRICS_PORT: '0', OTEL_RESOURCE_ATTRIBUTES: 'service.instance.id=inst-fixed' });
  assert.equal(out.primary.id, 'inst-fixed');
  assert.deepEqual(out.primary.lines.filter((l) => l.includes('service.instance.id')), []);
  for (const w of out.workers) {
    assert.equal(w.id, 'inst-fixed');
    assert.equal(w.lines.filter((l) => ID_WARNING.test(l)).length, 1, w.lines.join('\n'));
  }
});

test('D4 A24 a code-provided id in a cluster worker is kept and warned', () => {
  const out = runCluster({ OTEL_HELPER_METRICS_PORT: '0', WORKER_OPTIONS: JSON.stringify({ resourceAttributes: { 'service.instance.id': 'code-id' } }) });
  assert.equal(out.primary.id, null);
  for (const w of out.workers) {
    assert.equal(w.id, 'code-id');
    assert.equal(w.lines.filter((l) => ID_WARNING.test(l)).length, 1, w.lines.join('\n'));
  }
});

test('D-JS14 a child_process.fork() child of a worker is not a cluster worker: an explicit id is not warned there', () => {
  const out = runCluster({ OTEL_HELPER_METRICS_PORT: '0', OTEL_RESOURCE_ATTRIBUTES: 'service.instance.id=inst-fixed', CHILD_FORK: '1' });
  for (const w of out.workers) {
    assert.equal(w.lines.filter((l) => ID_WARNING.test(l)).length, 1, w.lines.join('\n'));
    assert.equal(w.child.isWorker, false);
    assert.equal(w.child.id, 'inst-fixed');
    assert.deepEqual(w.child.lines.filter((l) => l.includes('service.instance.id')), []);
  }
});

test('D-JS14 a primary that also set up owns the port: the workers report EADDRINUSE and the port serves the primary', async () => {
  const [port] = await freePorts(1);
  const out = runCluster({ PRIMARY_SETUP: '1', OTEL_HELPER_METRICS_PORT: String(port), WAIT: 'bind-error', SCRAPE_PORTS: JSON.stringify([port]) });
  for (const w of out.workers) {
    assert.ok(w.lines.includes(PORT_WARNING(port)), w.lines.join('\n'));
    assert.ok(w.lines.some((l) => l.includes(`could not bind port ${port}`) && l.endsWith('error=EADDRINUSE')), w.lines.join('\n'));
  }
  for (const s of out.scrapes[port]) {
    assert.equal(s.status, 200);
    // The workers' counters never reach the primary's registry.
    assert.deepEqual(slotsIn(s.body), []);
  }
});

test('D-JS14 workers on one shared port: scrapes round-robin between them, each warned once', async () => {
  const [port] = await freePorts(1);
  const out = runCluster({ OTEL_HELPER_METRICS_PORT: String(port), WAIT: 'listening', SCRAPE_PORTS: JSON.stringify([port]) });
  for (const w of out.workers) assert.equal(w.lines.filter((l) => l === PORT_WARNING(port)).length, 1, w.lines.join('\n'));
  const seen = out.scrapes[port].map((s) => {
    assert.equal(s.status, 200);
    const slots = slotsIn(s.body);
    assert.equal(slots.length, 1, s.body);
    return slots[0];
  });
  assert.deepEqual([...new Set(seen)].sort(), [0, 1], `slots per scrape: ${seen.join(',')}`);
});

test('JS9 D-JS13 per-worker slot ports: each port serves its own worker, conditional warning once per worker', async () => {
  const ports = await freePorts(2);
  const out = runCluster({ OTEL_HELPER_METRICS_PORT: '0', SLOT_PORTS: JSON.stringify(ports), WAIT: 'listening', SCRAPE_PORTS: JSON.stringify(ports) });
  for (const w of out.workers) {
    const port = ports[w.slot];
    assert.equal(w.lines.filter((l) => l === PORT_WARNING(port)).length, 1, w.lines.join('\n'));
    assert.ok(!w.lines.some((l) => l.includes('could not bind')), w.lines.join('\n'));
    for (const s of out.scrapes[port]) {
      assert.equal(s.status, 200);
      assert.deepEqual(slotsIn(s.body), [w.slot], s.body);
    }
  }
});

test('JS9 D-JS13 a crashed worker re-forked on its slot serves the same port, with a new instance id', async () => {
  const ports = await freePorts(2);
  const out = runCluster({ OTEL_HELPER_METRICS_PORT: '0', SLOT_PORTS: JSON.stringify(ports), WAIT: 'listening', CRASH_SLOT: '1', SCRAPE_PORTS: JSON.stringify([ports[1]]) });
  assert.equal(out.crashed.length, 1);
  const [first] = out.crashed;
  const replacement = out.workers[1];
  assert.equal(first.slot, 1);
  assert.notEqual(replacement.pid, first.pid);
  // D4 trade-off: a restarted worker is a new process, so new series appear under a new id.
  assert.notEqual(replacement.id, first.id);
  for (const s of out.scrapes[ports[1]]) {
    assert.equal(s.status, 200);
    assert.deepEqual(pidsIn(s.body), [replacement.pid], s.body);
  }
});

// D-JS13 correction of 2026-11-03 (B12) and its fix round: worker.disconnect()
// is not a signal, so a SIGTERM-only flush does not run on it. A
// worker with nothing else open then ends on its own and the helper's
// beforeExit flush exports its spans (D-JS10, B12 fix round); a worker that
// something keeps alive (a timer, a pool) needs a `process.once('disconnect',
// …)` handler that awaits shutdown() and exits (HOW-TO §13, cluster and pm2).
// KEEPALIVE=1 holds such a handle open; FLUSH_ON_DISCONNECT=1 adds the handler.
const DISCONNECT_SCRIPT = (helperPath) => `'use strict';
const cluster = require('node:cluster');
if (cluster.isPrimary) {
  setTimeout(() => {
    console.log('primary deadline (20 s) exceeded');
    for (const id in cluster.workers) cluster.workers[id].process.kill('SIGKILL');
    process.exit(1);
  }, 20_000).unref();
  const w = cluster.fork();
  w.on('message', () => w.disconnect());
  w.on('exit', (code) => { console.log('worker-exit ' + code); });
} else {
  const h = require(${JSON.stringify(helperPath)});
  h.setup({ serviceName: 'svc', metricExporters: [] });
  h.getTracer('cluster-test').startSpan('disconnect-probe-span').end();
  process.once('SIGTERM', () => { h.shutdown().finally(() => process.exit(0)); });
  if (process.env.KEEPALIVE === '1') setInterval(() => {}, 1000);
  if (process.env.FLUSH_ON_DISCONNECT === '1') process.once('disconnect', () => { h.shutdown().finally(() => process.exit(0)); });
  process.send('ready');
}
`;

function runDisconnect(env) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'otel-helper-cluster-'));
  const script = path.join(dir, 'disconnect.cjs');
  fs.writeFileSync(script, DISCONNECT_SCRIPT(require.resolve('@staffops/otel-helper')));
  try {
    // The console exporter behind the batch processor, its delay far beyond
    // the run: a span reaches stdout only through a flush.
    const res = runNode([script], { env: { ...BASE_ENV, OTEL_TRACES_EXPORTER: 'console', OTEL_BSP_SCHEDULE_DELAY: '60000', ...env } });
    assert.equal(res.status, 0, `${res.stdout}\n${res.stderr}`);
    assert.match(res.stdout, /^worker-exit 0$/m, res.stdout);
    return res.stdout;
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('D-JS13 a worker the primary disconnects, with nothing else open, ends on its own and the beforeExit flush exports its spans', () => {
  assert.match(runDisconnect({}), /name: 'disconnect-probe-span'/);
});

test('D-JS13 a disconnected worker kept alive by a handle exits and flushes through its disconnect handler', () => {
  assert.match(runDisconnect({ KEEPALIVE: '1', FLUSH_ON_DISCONNECT: '1' }), /name: 'disconnect-probe-span'/);
});
