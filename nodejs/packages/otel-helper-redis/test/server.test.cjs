'use strict';
// @staffops/otel-helper-redis against a real Redis (task B10b, spec JS8): the
// command matrix of redis.test.cjs (test/fixtures/scenario.cjs) — node-redis 6
// over RESP3, ioredis 6 — with the server's own replies and error texts.
//
// Server: REDIS_TEST_URL, `redis://default:<password>@host:port/0` (throwaway
// password; testing/docker-compose.yml service redis-test). Unset: skipped,
// loudly; with REDIS_TEST_REQUIRE=1 (compose, CI) a missing URL fails instead.
const test = require('node:test');
const assert = require('node:assert/strict');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { redisInstrumentation } = require('@staffops/otel-helper-redis');
const { runScenario, assertScenario } = require('./fixtures/scenario.cjs');

const URL_ = process.env.REDIS_TEST_URL;
const REQUIRE = process.env.REDIS_TEST_REQUIRE === '1';
const REFUSED = 'redis://127.0.0.1:1';
const opts = { skip: URL_ === undefined && !REQUIRE ? 'REDIS_TEST_URL unset' : false };

const instrumentation = redisInstrumentation();

test('the Redis URL is configured (REDIS_TEST_REQUIRE=1 makes it mandatory)', (t) => {
  if (!REQUIRE && URL_ === undefined) {
    t.skip('REDIS_TEST_URL unset: real-server tests skipped (run node-tests in testing/docker-compose.yml)');
    return;
  }
  assert.ok(URL_, 'REDIS_TEST_URL');
  assert.ok(new URL(URL_).password, 'the URL carries the password (the NOAUTH and no-secret cases need one)');
});

test('every command form against a real Redis: sanitized statements, no secrets, ERROR without description', opts, async () => {
  for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  testing.reset();
  testing.setDiagnosticSink(() => undefined);
  const exporter = new InMemorySpanExporter();
  helper.setup({ instrumentations: [instrumentation], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  try {
    await runScenario({ url: URL_, refusedUrl: REFUSED });
    assertScenario(exporter.getFinishedSpans(), { url: URL_, refusedUrl: REFUSED });
  } finally {
    await helper.shutdown();
    testing.reset();
  }
});

test('one span per command issued before ready (ioredis offline queue replay), and after ready; node-redis before connect resolves', opts, async () => {
  for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  testing.reset();
  testing.setDiagnosticSink(() => undefined);
  const exporter = new InMemorySpanExporter();
  helper.setup({ instrumentations: [instrumentation], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  const IORedis = require('ioredis');
  const { createClient } = require('redis');
  const sets = () => exporter.getFinishedSpans().filter((s) => s.name === 'SET');
  try {
    // Before ready: queued offline, replayed through sendCommand on ready.
    const early = new IORedis(URL_);
    let readyAt;
    early.once('ready', () => { readyAt = process.hrtime.bigint(); });
    const issuedAt = process.hrtime.bigint();
    await early.set('b14-offline', '1');
    await early.quit();
    assert.ok(readyAt !== undefined && readyAt > issuedAt, 'the command was issued before ready');
    assert.equal(sets().length, 1, `ioredis before ready: ${sets().length} SET spans`);
    // The kept span is the first one: it starts before ready (queueing included).
    const [span] = sets();
    const startNs = BigInt(span.startTime[0]) * 1_000_000_000n + BigInt(span.startTime[1]);
    const endNs = BigInt(span.endTime[0]) * 1_000_000_000n + BigInt(span.endTime[1]);
    assert.ok(endNs - startNs >= readyAt - issuedAt - 1_000_000n, 'the span covers the wait for ready');

    exporter.reset();
    const late = new IORedis(URL_);
    await new Promise((resolve) => late.once('ready', resolve));
    await late.set('b14-online', '1');
    await late.set('b14-online', '2');
    await late.quit();
    assert.equal(sets().length, 2, 'ioredis after ready: one span per command');

    exporter.reset();
    const nodeRedis = createClient({ url: URL_ });
    const connecting = nodeRedis.connect();
    await nodeRedis.set('b14-node', '1');
    await connecting;
    await nodeRedis.quit();
    assert.equal(sets().length, 1, 'node-redis before connect resolves: one span');
  } finally {
    await helper.shutdown();
    testing.reset();
  }
});
