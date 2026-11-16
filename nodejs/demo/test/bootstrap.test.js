'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const { bootstrap } = require('../lib/bootstrap');

function recorder() {
  const calls = [];
  return {
    calls,
    setup: (opts) => { calls.push(['setup', opts]); return { n: calls.length }; },
    factory: () => { calls.push(['factory']); return ['inst']; },
  };
}

test('bootstrap: same service.instance.id in both setups, factory created after the first shutdown', async () => {
  const r = recorder();
  const result = await bootstrap(r.setup, async (ms) => { r.calls.push(['shutdown', ms]); }, r.factory, { env: {}, uuid: () => 'id-1' });
  const ra = { resourceAttributes: { 'service.instance.id': 'id-1' } };
  assert.deepEqual(r.calls, [['setup', ra], ['shutdown', 1000], ['factory'], ['setup', { ...ra, instrumentations: ['inst'] }]]);
  assert.deepEqual(result, { n: 4 });
});

test('bootstrap keeps an instance id from OTEL_RESOURCE_ATTRIBUTES', async () => {
  const r = recorder();
  await bootstrap(r.setup, async () => {}, r.factory, { env: { OTEL_RESOURCE_ATTRIBUTES: 'a=b,service.instance.id=x' }, uuid: () => { throw new Error('unused'); } });
  assert.deepEqual(r.calls[0], ['setup', {}]);
  assert.deepEqual(r.calls[2], ['setup', { instrumentations: ['inst'] }]);
});

test('bootstrap continues when the first shutdown fails (one line, error name only)', async () => {
  const r = recorder();
  const lines = [];
  const result = await bootstrap(r.setup, async () => { throw new RangeError('secret detail'); }, r.factory, { env: {}, uuid: () => 'id-2', warn: (l) => lines.push(l) });
  assert.deepEqual(lines, ['otel-libs-demo: first telemetry shutdown failed (RangeError); continuing']);
  assert.deepEqual(r.calls.at(-1), ['setup', { resourceAttributes: { 'service.instance.id': 'id-2' }, instrumentations: ['inst'] }]);
  assert.ok(result);
});

test('bootstrap defaults: process.env, randomUUID, stderr', async (t) => {
  const r = recorder();
  const written = [];
  t.mock.method(process.stderr, 'write', (chunk) => { written.push(String(chunk)); return true; });
  await bootstrap(r.setup, async () => { throw new Error('x'); }, r.factory);
  assert.match(written.join(''), /first telemetry shutdown failed \(Error\); continuing\n/);
  const id = r.calls[0][1].resourceAttributes?.['service.instance.id'];
  if (id !== undefined) assert.match(id, /^[0-9a-f-]{36}$/);
});
