// redis.test.cjs fixture: the ES module app, loaded after esm-preload.mjs.
import { createClient } from 'redis';
import IORedis from 'ioredis';
import { shutdown } from '@staffops/otel-helper';
import { getState } from '@staffops/otel-helper/testing';

const url = process.env.REDIS_FAKE_URL;
const client = createClient({ url });
client.on('error', () => undefined);
await client.connect();
await client.set('k', 'v');
await client.quit();
const io = new IORedis(url, { lazyConnect: true });
await io.connect();
await io.set('k', 'v');
await io.quit();
const spans = globalThis.__fixtureExporter.getFinishedSpans().map((s) => ({ name: s.name, scope: s.instrumentationScope.name, attributes: s.attributes }));
const hooks = getState()?.hooks ?? null;
await shutdown();
console.log(JSON.stringify({ hooks, spans }));
