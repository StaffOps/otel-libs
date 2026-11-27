'use strict';
// redis.test.cjs fixture: redis and ioredis loaded — and a client of each
// built — before the instrumentation exists (FIXTURE_NO_PRELOAD=1: after
// setup(), for the semconv mode test). Commands go to REDIS_FAKE_URL.
const preload = process.env.FIXTURE_NO_PRELOAD !== '1';
const early = preload ? { redis: require('redis'), IORedis: require('ioredis') } : undefined;
const url = process.env.REDIS_FAKE_URL;
const clients = early === undefined ? undefined : { c: early.redis.createClient({ url }), r: new early.IORedis(url, { lazyConnect: true }) };
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const { redisInstrumentation } = require('@staffops/otel-helper-redis');

const exporter = new InMemorySpanExporter();
helper.setup({ instrumentations: [redisInstrumentation({ redis: true, ioredis: true })], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
const { c, r } = clients ?? { c: require('redis').createClient({ url }), r: new (require('ioredis'))(url, { lazyConnect: true }) };

(async () => {
  c.on('error', () => undefined);
  await c.connect();
  await c.set('k', 'v');
  await c.quit();
  await r.connect();
  await r.set('k', 'v');
  await r.quit();
  const spans = exporter.getFinishedSpans().map((s) => ({ name: s.name, scope: s.instrumentationScope.name, attributes: s.attributes }));
  await helper.shutdown();
  console.log(JSON.stringify({ preloaded: preload, spans }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
