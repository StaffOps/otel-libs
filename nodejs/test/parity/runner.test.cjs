'use strict';
// Parity fixture runner for Node.js (design D-JS12; ADR-008; format and
// semantics: parity/README.md). Port of python/tests/test_parity.py; the
// schema, matcher and well-formedness rules mirror it and
// go/parity_match_test.go.
//
// Each case in parity/cases.json is one test. Status `pass` must pass;
// `xfail:<task>` must fail on a behavior mismatch (strict: an unexpected pass
// fails, so the task that fixes the behavior flips the status);
// `skip:<reason>` is skipped. Fixture or adapter errors (unknown kind, expect
// key outside the snapshot schema, unmapped code key, a helper API still
// pending, driver crash) fail whatever the status.
//
// Every case runs in its own process (driver.cjs): OTel globals are set-once.
// Non-export cases run concurrently (at most min(8, 2 x CPUs) children), then
// export cases one at a time (they share the fake receiver's request log).
//
//   node --test "test/parity/*.test.cjs"            # from nodejs/ (npm run test:parity)
//   node --test --test-name-pattern='export.grpc_plaintext' "test/parity/*.test.cjs"
//
// The quoted glob matters: `node --test test/parity/` fails on Node 22/24.
//
// Environment: FAKEOTLP_* (testing/README.md), PARITY_REQUIRE_FAKEOTLP (a
// receiver-dependent case fails instead of skipping), PARITY_RELEASE (release
// gate: no `skip:B…` left for nodejs), PARITY_CASES (fixture path).

const test = require('node:test');
const { describe } = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const tls = require('node:tls');
const {
  CASES_PATH, PERMANENT_SKIPS, SCHEMA, NeedsReceiver,
  loadCases, nodeStatus, validateCase, receiver, expand, destIps, childEnv, runCase, matchExpect, verdict,
} = require('./harness.cjs');

// ── tests ───────────────────────────────────────────────────────────────

const CASES = loadCases();

const CASE_TIMEOUT_MS = 150_000; // above the 120 s child timeout, so the harness reports first
const NON_EXPORT_CONCURRENCY = Math.min(8, os.availableParallelism() * 2);
const TASKS_PATH = process.env.PARITY_TASKS
  || path.resolve(path.dirname(CASES_PATH), '..', 'specs', 'nodejs-otel-helper', 'tasks.md');

async function runParityCase(t, c) {
  // A malformed case fails before a child is spawned for it.
  const errs = validateCase(c);
  assert.deepEqual(errs, [], `fixture error (fails regardless of status): ${errs.join('; ')}`);
  const snap = await runCase(c);
  const v = verdict(c, snap);
  if (v.skip) {
    t.skip(v.skip);
    return;
  }
  if (v.xfail) t.diagnostic(`expected failure (${nodeStatus(c)}): ${v.xfail}`);
  assert.ok(v.ok, v.reason);
}

function caseTest(c) {
  const status = nodeStatus(c);
  const options = status.startsWith('skip:') ? { skip: status } : { timeout: CASE_TIMEOUT_MS };
  test(c.id, options, (t) => runParityCase(t, c));
}

describe('parity: non-export cases (concurrent)', { concurrency: NON_EXPORT_CONCURRENCY }, () => {
  for (const c of CASES.filter((x) => x.kind !== 'export')) caseTest(c);
});

describe('parity: export cases (serial, shared receiver log)', { concurrency: 1 }, () => {
  for (const c of CASES.filter((x) => x.kind === 'export')) caseTest(c);
});

describe('parity fixture', () => {
  test('is well formed (ids unique, every case valid)', () => {
    const ids = CASES.map((c) => c.id);
    assert.equal(new Set(ids).size, ids.length, 'duplicate case ids');
    const problems = Object.fromEntries(CASES.map((c) => [c.id, validateCase(c)]).filter(([, e]) => e.length));
    assert.deepEqual(problems, {});
  });

  test('every nodejs skip names its task (skip:B<n>-<reason>) or a recorded difference', () => {
    const bad = CASES.map((c) => [c.id, nodeStatus(c)])
      .filter(([, s]) => s.startsWith('skip:') && !/^skip:B\d+[a-z]?-[A-Za-z0-9._-]+$/.test(s) && !PERMANENT_SKIPS.has(s));
    assert.deepEqual(bad, []);
  });

  test('every skip:B<n> names a task that exists in specs/nodejs-otel-helper/tasks.md', () => {
    // Task ids are the first token of each table row ("| B4a Endpoints… |").
    const ids = new Set([...fs.readFileSync(TASKS_PATH, 'utf8').matchAll(/^\| (B\d+[a-z]?) /gm)].map((m) => m[1]));
    assert.ok(ids.size > 0, `no task ids found in ${TASKS_PATH}`);
    const unknown = CASES.map((c) => [c.id, nodeStatus(c), /^skip:(B\d+[a-z]?)-/.exec(nodeStatus(c))?.[1]])
      .filter(([, , task]) => task !== undefined && !ids.has(task));
    assert.deepEqual(unknown, []);
  });

  test('every code key in the fixture maps to a setup() option', () => {
    const { mapOptions } = require('./driver.cjs');
    for (const c of CASES) assert.doesNotThrow(() => mapOptions(c.code), c.id);
  });

  test('release gate: no nodejs case still skipped pending a task', { skip: process.env.PARITY_RELEASE ? false : 'set PARITY_RELEASE=1 for the release gate' }, () => {
    const pending = CASES.filter((c) => nodeStatus(c).startsWith('skip:B')).map((c) => `${c.id}: ${nodeStatus(c)}`);
    assert.deepEqual(pending, []);
  });
});

describe('parity harness (matcher and schema are test infrastructure)', () => {
  const base = { id: 'x', env: {}, code: {}, status: { go: 'pass', python: 'pass', dotnet: 'xfail:phase-c', nodejs: 'skip:B3-resolver' } };

  test('operators', () => {
    const snap = { a: 'unknown_service:node', n: 3, log: 'x OTEL_FOO y', diagnostics: 'OTEL_FOO\nOTEL_FOO' };
    assert.deepEqual(matchExpect({ a: { prefix: 'unknown_service' }, n: 3, warning: 'OTEL_FOO' }, snap), []);
    assert.deepEqual(matchExpect({ b: { absent: true } }, snap), []);
    assert.ok(matchExpect({ a: { absent: false } }, {}).length);
    assert.deepEqual(matchExpect({ a: { all: [{ regex: 'node$' }, { not_contains: 'go' }] } }, snap), []);
    assert.deepEqual(matchExpect({ warning_count: { OTEL_FOO: 2 } }, snap), []);
    assert.ok(matchExpect({ warning_count: { OTEL_FOO: 1 } }, snap).length);
    assert.ok(matchExpect({ a: { not_contains: 'node' } }, snap).length);
    assert.ok(matchExpect({ a: { regex: '(' } }, snap).length); // bad regex → mismatch, not a crash
    assert.ok(matchExpect({ n: 4 }, snap).length);
    assert.ok(matchExpect({ warning: ['OTEL_FOO', 'OTEL_BAR'] }, snap).length);
    assert.deepEqual(matchExpect({ r: { 'service.name': 'x' } }, { r: { 'service.name': 'x', other: 'y' } }), []);
    assert.ok(matchExpect({ r: { 'service.name': 'x' } }, { r: 'flat' }).length);
    assert.deepEqual(matchExpect({ l: ['a', 'b'] }, { l: ['a', 'b'] }), []);
    assert.ok(matchExpect({ l: ['a', 'b'] }, { l: ['b', 'a'] }).length);
    assert.ok(matchExpect({ s: true }, { s: 1 }).length);
    assert.deepEqual(matchExpect({ t: { lte: 500, gte: 1 } }, { t: 499 }), []);
    assert.ok(matchExpect({ t: { lte: 500 } }, { t: 9999 }).length);
    assert.ok(matchExpect({ t: { gte: 1 } }, { t: '5' }).length);
    // Text matchers apply to strings only (no JSON/str() form of other values).
    assert.ok(matchExpect({ o: { regex: 'k' } }, { o: { k: 1 } }).length);
    assert.ok(matchExpect({ n: { prefix: '3' } }, { n: 3 }).length);
    assert.ok(matchExpect({ n: { not_contains: 'x' } }, { n: 3 }).length);
    assert.deepEqual(matchExpect({ n: { not_contains: 'x' } }, {}), []);
    // Regexes compile in Unicode mode; inline flags are not part of the dialect.
    assert.ok(matchExpect({ a: { regex: '(?i)UNKNOWN' } }, snap).length);
    assert.deepEqual(matchExpect({ a: { regex: '^\\p{L}+_' } }, snap), []);
  });

  test('an unexpected error fails unless expected', () => {
    assert.deepEqual(matchExpect({}, { error: 'boom' }), ['unexpected error: boom']);
    assert.deepEqual(matchExpect({ error: 'boo' }, { error: 'boom' }), []);
    assert.ok(matchExpect({ error: 'zzz' }, { error: 'boom' }).length);
    assert.ok(matchExpect({ error: { absent: true } }, { error: 'boom' }).length);
  });

  test('received', () => {
    const snap = { received: { traces: [{ port: 4318, path: '/v1/traces' }], metrics: [] } };
    assert.deepEqual(matchExpect({ received: { traces: { port: 4318 }, metrics: { absent: true } } }, snap), []);
    assert.ok(matchExpect({ received: { metrics: {} } }, snap).length);
    assert.ok(matchExpect({ received: { traces: { port: 443 } } }, snap).length);
    assert.ok(matchExpect({ received: { traces: { absent: true } } }, snap).length);
    assert.ok(matchExpect({ received: { logs: {} } }, {}).length);
  });

  test('schema rejects fixture typos', () => {
    assert.ok(validateCase({ ...base, kind: 'bogus', expect: {} }).length);
    assert.ok(validateCase({ ...base, kind: 'toString', expect: {} }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: { resorce: {} } }).length);
    assert.ok(validateCase({ ...base, kind: 'export', expect: { received: { trace: {} } } }).length);
    assert.ok(validateCase({ ...base, kind: 'export', expect: { received: { traces: { prot: 'grpc' } } } }).length);
    assert.ok(validateCase({ ...base, kind: 'export', expect: { received: { traces: 'x' } } }).length);
    assert.deepEqual(validateCase({ ...base, kind: 'resolve', expect: { resource: {} } }), []);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: { log: { regex: 'x', contians: 'y' } } }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: { log: { all: 'x' } } }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: { log: { lte: '1' } } }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: { log: { regex: '(' } } }).length);
    assert.ok(validateCase({ ...base, kind: 'export', expect: { received: 'nope' } }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: { warning_count: { x: '1' } } }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: { warning: [1] } }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: [] }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: {}, stauts: {} }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: {}, status: { ...base.status, 'python@sdk>=x': 'pass' } }).length);
    assert.deepEqual(validateCase({ ...base, kind: 'resolve', expect: {}, status: { ...base.status, 'python@sdk>=1.45': 'pass' } }), []);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: {}, status: { ...base.status, nodejs: 'skip:' } }).length);
    assert.ok(validateCase({ ...base, kind: 'resolve', expect: {}, status: { ...base.status, 'nodejs@node>=26': 'skip:x' } }).length);
    const noEnv = { ...base };
    delete noEnv.env;
    assert.ok(validateCase({ ...noEnv, kind: 'resolve', expect: {} }).length);
    assert.ok(validateCase({ ...base, kind: 'sampler', expect: {}, input: { scenario: 1 } }).length);
    assert.ok(validateCase({ ...base, kind: 'sampler', expect: {}, input: { bogus: 'x' } }).length);
    assert.ok(validateCase({ ...base, kind: 'http_server', expect: {}, input: { requests: [1] } }).length);
    assert.ok(validateCase({ ...base, kind: 'http_server', expect: {}, input: { child_span: 'yes' } }).length);
  });

  test('verdict: strict xfail, hard failures, receiver skips', () => {
    const c = { ...base, kind: 'resolve', expect: { resource: { 'service.name': 'a' } } };
    const okSnap = { resource: { 'service.name': 'a' } };
    const badSnap = { resource: { 'service.name': 'b' } };
    const xfail = { ...c, status: { ...c.status, nodejs: 'xfail:B3' } };
    const pass = { ...c, status: { ...c.status, nodejs: 'pass' } };
    assert.equal(verdict(pass, okSnap).ok, true);
    assert.equal(verdict(pass, badSnap).ok, false);
    assert.equal(verdict(xfail, okSnap).ok, false); // unexpected pass
    assert.equal(verdict(xfail, badSnap).ok, true);
    assert.equal(verdict(xfail, { adapter_error: 'x' }).ok, false); // never satisfies an xfail
    assert.equal(verdict(xfail, { adapter_pending: 'x' }).ok, false);
    assert.equal(verdict(xfail, { _crash: 'x' }).ok, false);
    assert.equal(verdict({ ...xfail, kind: 'bogus' }, badSnap).ok, false);
    assert.equal(verdict(pass, { _needs: 'FAKEOTLP_HOST' }, { requireReceiver: true }).ok, false);
    assert.ok(verdict(pass, { _needs: 'FAKEOTLP_HOST' }, { requireReceiver: false }).skip);
  });

  test('placeholders, receiver requirements and child env', async () => {
    const recv = receiver({ FAKEOTLP_HOST: 'h', FAKEOTLP_HOST_TLS: 't', FAKEOTLP_HOST_IPV6: '[::1]', FAKEOTLP_CA: '/ca' });
    assert.deepEqual(
      expand({ a: 'https://{receiver_tls}', b: ['{receiver}:1', '[{receiver_ipv6}]'], c: '{receiver_ca}', d: 1 }, recv),
      { a: 'https://t', b: ['h:1', '[[::1]]'], c: '/ca', d: 1 },
    );
    assert.equal(expand('plain', null), 'plain');
    assert.throws(() => expand('{receiver}', null), NeedsReceiver);
    assert.throws(() => expand('{receiver_tls}', receiver({ FAKEOTLP_HOST: 'h' })), /FAKEOTLP_HOST_TLS/);
    assert.throws(() => expand('{receiver_ipv6}', receiver({ FAKEOTLP_HOST: 'h' })), /FAKEOTLP_HOST_IPV6/);
    const saved = { ...process.env };
    try {
      process.env.OTEL_SERVICE_NAME = 'leak';
      process.env.SERVICE_NAME = 'leak';
      process.env.ENVIRONMENT = 'leak';
      process.env.OTEL_HELPER_METRICS_PORT = '9464';
      const env = await childEnv({ kind: 'resolve', env: { OTEL_EXPORTER_OTLP_ENDPOINT: 'http://{receiver}:4317' } }, recv);
      assert.equal(env.OTEL_SERVICE_NAME, undefined);
      assert.equal(env.SERVICE_NAME, undefined);
      assert.equal(env.ENVIRONMENT, undefined);
      assert.equal(env.OTEL_HELPER_METRICS_PORT, '0');
      assert.equal(env.OTEL_EXPORTER_OTLP_ENDPOINT, 'http://h:4317');
      const override = await childEnv({ kind: 'resolve', env: { OTEL_HELPER_METRICS_PORT: '9999' } }, null);
      assert.equal(override.OTEL_HELPER_METRICS_PORT, '9999');
      await assert.rejects(childEnv({ kind: 'export', env: {} }, null), NeedsReceiver);
    } finally {
      for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
      Object.assign(process.env, saved);
    }
    const local = receiver({ FAKEOTLP_HOST: '127.0.0.1', FAKEOTLP_HOST_TLS: '', FAKEOTLP_HOST_IPV6: '[::1]' });
    assert.equal(await destIps(local), '127.0.0.1,::1');
    assert.deepEqual(await runCase({ id: 'x', kind: 'export', env: {}, code: {} }, { env: {} }), { _needs: 'FAKEOTLP_HOST' });
  });

  test('driver: reports pending helper APIs, adapter errors and crashes', async () => {
    const env = { PATH: process.env.PATH ?? '' };
    const pending = await runCase({ id: 'p', kind: 'metrics_scrape', env: { OTEL_SERVICE_NAME: 'svc' }, code: {}, expect: {} }, { env });
    assert.match(String(pending.adapter_pending), /metricsHandler\(\) yet \(task B7\)/);
    // setup() exists since B3: a resolve case runs and reports the resource.
    const resolved = await runCase({ id: 'r', kind: 'resolve', env: { OTEL_SERVICE_NAME: 'svc' }, code: {}, expect: {} }, { env });
    assert.equal(resolved.resource?.['service.name'], 'svc', JSON.stringify(resolved));
    const unknownKind = await runCase({ id: 'k', kind: 'bogus', env: {}, code: {} }, { env });
    assert.match(String(unknownKind.adapter_error), /unknown kind/);
    const driverModule = require('./driver.cjs');
    assert.throws(() => driverModule.mapOptions({ bogus_key: 1, service_name: 's' }), /unsupported code key\(s\) bogus_key/);
    assert.deepEqual(driverModule.mapOptions({ service_name: 's', debug: 1, health_paths: ['/h'] }), { debug: true, healthPaths: ['/h'], serviceName: 's' });
    assert.equal(driverModule.grpcTimeoutMs('1500m'), 1500);
    assert.equal(driverModule.grpcTimeoutMs('2S'), 2000);
    assert.equal(driverModule.grpcTimeoutMs('x'), undefined);
    assert.equal(driverModule.signalOf('grpc', '/opentelemetry.proto.collector.trace.v1.TraceService/Export'), 'traces');
    assert.equal(driverModule.signalOf('http', '/otlp/v1/metrics'), 'metrics');
    assert.equal(driverModule.signalOf('http', '/x'), 'other');
    assert.deepEqual(Object.keys(driverModule.KINDS).sort(), Object.keys(SCHEMA).sort());
    // Every kind is wired, never "unknown kind": kinds whose helper API exists
    // run (no adapter status); the others report the task that adds it.
    const runnable = new Set(['resolve', 'sampler', 'propagation', 'fork']);
    for (const kind of Object.keys(SCHEMA)) {
      if (kind === 'export') continue; // needs the receiver; covered by the export check below
      const res = await runCase({ id: kind, kind, env: {}, code: {}, input: { scenario: 'root', requests: [], method: '/x' } }, { env });
      if (runnable.has(kind)) {
        assert.equal(res.adapter_pending, undefined, `${kind}: ${JSON.stringify(res)}`);
        assert.equal(res.adapter_error, undefined, `${kind}: ${JSON.stringify(res)}`);
        assert.equal(res._crash, undefined, `${kind}: ${JSON.stringify(res)}`);
      } else {
        assert.match(String(res.adapter_pending), /task B\d/, `${kind}: ${JSON.stringify(res)}`);
      }
    }
    // Sink metadata: helper_log = origin helper (any level); diagnostics = warn and above (any origin).
    assert.deepEqual(driverModule.classify({ origin: 'helper', level: 'warn' }), { diagnostics: true, helperLog: true });
    assert.deepEqual(driverModule.classify({ origin: 'helper', level: 'info' }), { diagnostics: false, helperLog: true });
    assert.deepEqual(driverModule.classify({ origin: 'diag', level: 'error' }), { diagnostics: true, helperLog: false });
    assert.deepEqual(driverModule.classify({ origin: 'diag', level: 'debug' }), { diagnostics: false, helperLog: false });
    assert.deepEqual(driverModule.classify(undefined), { diagnostics: false, helperLog: false });
    // The driver is not a test file: without its two arguments it refuses to run.
    const usage = spawnSync(process.execPath, [driverModule.DRIVER], { encoding: 'utf8' });
    assert.equal(usage.status, 2);
    assert.match(usage.stderr, /usage: node driver\.cjs <case\.json> <out\.json>/);
    const crash = await runCase({ id: 'c', kind: 'resolve', env: { NODE_OPTIONS: '--require=/nonexistent-parity-preload.cjs' }, code: {} }, { env });
    assert.match(String(crash._crash), /driver crashed/);
    // An unexpected harness exception is reported as a crash, never thrown into node:test.
    assert.match(String((await runCase(null))._crash), /^harness error: TypeError/);
  });

  test('driver: export kind talks to the receiver admin API and reports what it received', { skip: process.env.FAKEOTLP_ADMIN ? false : 'needs FAKEOTLP_ADMIN' }, async () => {
    // No exporters before B4: nothing reaches the receiver, but the snapshot is built.
    const res = await runCase({ id: 'e', kind: 'export', env: { OTEL_SERVICE_NAME: 'svc' }, code: {}, expect: {} });
    assert.equal(res.adapter_pending, undefined, JSON.stringify(res));
    assert.deepEqual(res.received?.traces, [], JSON.stringify(res));
  });
});

// D-JS14 F5: Node 26 refuses an IP literal as TLS ServerName, so the Node legs
// address the receivers by DNS name (testing/README.md). This proves, on every
// Node version, that the names resolve to the receiver addresses and that the
// receiver certificate carries them as SANs.
describe('fake receiver addressing (D-JS14 F5)', { skip: process.env.FAKEOTLP_HOST ? false : 'needs FAKEOTLP_HOST' }, () => {
  function handshake(host, port) {
    const ca = fs.readFileSync(process.env.FAKEOTLP_CA);
    return new Promise((resolve, reject) => {
      const opts = { host, port, ca, ALPNProtocols: ['h2', 'http/1.1'] };
      if (!net.isIP(host)) opts.servername = host;
      const sock = tls.connect(opts, () => {
        const ok = sock.authorized;
        const err = sock.authorizationError;
        sock.end();
        resolve({ ok, err });
      });
      sock.on('error', reject);
      sock.setTimeout(10_000, () => sock.destroy(new Error(`TLS handshake with ${host}:${port} timed out`)));
    });
  }

  test('TLS receivers verify against the fixture CA by the configured host', { skip: process.env.FAKEOTLP_CA ? false : 'needs FAKEOTLP_CA' }, async () => {
    for (const [host, port] of [[process.env.FAKEOTLP_HOST, 14317], [process.env.FAKEOTLP_HOST_TLS, 4317]]) {
      assert.ok(host, 'FAKEOTLP_HOST / FAKEOTLP_HOST_TLS must be set');
      const { ok, err } = await handshake(host, port);
      assert.ok(ok, `${host}:${port}: ${err}`);
    }
  });

  test('TLS receivers are addressed by DNS name under PARITY_REQUIRE_FAKEOTLP', { skip: process.env.PARITY_REQUIRE_FAKEOTLP ? false : 'needs PARITY_REQUIRE_FAKEOTLP' }, () => {
    for (const v of ['FAKEOTLP_HOST', 'FAKEOTLP_HOST_TLS']) assert.equal(net.isIP(process.env[v] ?? ''), 0, `${v}=${process.env[v]} must be a DNS name`);
  });
});

