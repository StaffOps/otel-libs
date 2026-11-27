'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { ROOT_CONTEXT, context } = require('@opentelemetry/api');
const { isTracingSuppressed } = require('@opentelemetry/core');

const { waitUntil, withoutTracing } = require('../lib/quiet');

test('withoutTracing runs fn in a context with tracing suppressed', (t) => {
  let captured;
  t.mock.method(context, 'with', (ctx, fn) => { captured = ctx; return fn(); });
  assert.equal(withoutTracing(() => 7), 7);
  assert.equal(isTracingSuppressed(captured), true);
  assert.equal(isTracingSuppressed(ROOT_CONTEXT), false);
});

test('waitUntil: retries misses and rejections, one onWait, resolves true', async () => {
  const answers = [false, 'error', true];
  const waits = [];
  const pauses = [];
  const ok = await waitUntil(async () => {
    const a = answers.shift();
    if (a === 'error') throw new Error('down');
    return a;
  }, { intervalMs: 5, onWait: (n) => waits.push(n), pause: async (ms) => pauses.push(ms) });
  assert.equal(ok, true);
  assert.deepEqual(waits, [1]);
  assert.deepEqual(pauses, [5, 5]);
});

test('waitUntil stops when asked and uses the defaults', async () => {
  let stop = false;
  const ok = await waitUntil(async () => { stop = true; return false; }, { shouldStop: () => stop, pause: async () => {} });
  assert.equal(ok, false);
  assert.equal(await waitUntil(async () => true), true);
  let n = 0;
  assert.equal(await waitUntil(async () => (n += 1) > 1, { intervalMs: 1 }), true);
});
