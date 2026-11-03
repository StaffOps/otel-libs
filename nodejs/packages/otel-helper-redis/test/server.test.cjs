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
