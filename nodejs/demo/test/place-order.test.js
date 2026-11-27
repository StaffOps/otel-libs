'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const { CODE } = require('../lib/grpc-status');
const { placeOrderInSpan } = require('../lib/place-order');

/** A fake of the helper's withSpan: records the call, runs fn, notes how it settled, rethrows. */
function fakeWithSpan(calls) {
  return async (name, fn, options) => {
    const call = { name, options, inside: true };
    calls.push(call);
    try {
      const out = await fn({});
      call.result = out;
      return out;
    } catch (err) {
      call.thrown = err;
      throw err;
    } finally {
      call.inside = false;
    }
  };
}

function deps(db, overrides = {}) {
  const calls = [];
  const points = [];
  const logs = [];
  const insideSpan = () => calls.length > 0 && calls[calls.length - 1].inside;
  return {
    calls,
    points,
    logs,
    withSpan: fakeWithSpan(calls),
    tracer: { name: 'otel-libs-demo' },
    db,
    run: 'r1',
    log: { info: (fields, msg) => logs.push({ fields, msg }) },
    processed: { add: (v, a) => points.push({ metric: 'processed', v, a, inside: insideSpan() }) },
    duration: { record: (v, a) => points.push({ metric: 'duration', v, a, inside: insideSpan() }) },
    now: (() => { let t = 0; return () => (t += 5); })(),
    ...overrides,
  };
}

const okDb = () => ({
  rows: {},
  async upsert(id, customer, amount, scenario) { this.rows[id] = { id, customer, amount, scenario }; },
  async insert() { throw Object.assign(new Error('duplicate order dup-r1'), { code: '23505' }); },
  async select(id) { return this.rows[id]; },
});

test('ok: reply, INFO record, ok metrics inside the span, span options', async () => {
  const d = deps(okDb());
  const out = await placeOrderInSpan(d, { order_id: 'ok-1', customer: 'c', amount: 11, scenario: 'ok' });
  assert.deepEqual(out, { reply: { order_id: 'ok-1', status: 'processed' } });
  assert.equal(d.calls.length, 1);
  assert.equal(d.calls[0].name, 'demo.order.process');
  assert.deepEqual(d.calls[0].options, { tracer: d.tracer, attributes: { 'demo.scenario': 'ok', 'order.id': 'ok-1' } });
  assert.equal(d.calls[0].thrown, undefined);
  assert.deepEqual(d.logs, [{ fields: { 'order.id': 'ok-1', 'demo.scenario': 'ok' }, msg: 'order processed' }]);
  assert.deepEqual(d.points.map((p) => [p.metric, p.a, p.inside]), [
    ['duration', { 'demo.scenario': 'ok', 'demo.outcome': 'ok' }, true],
    ['processed', { 'demo.scenario': 'ok', 'demo.outcome': 'ok' }, true],
  ]);
  assert.equal(d.points[0].v, 0.005);
});

for (const [scenario, request, type, code, details] of [
  ['business_error', { order_id: 'ord-3', customer: 'c', amount: 5000, scenario: 'business_error' },
    'OrderRejectedError', CODE.FAILED_PRECONDITION, 'order rejected'],
  ['db_error', { order_id: 'ord-6', customer: 'c', amount: 20, scenario: 'db_error' },
    'OrderExistsError', CODE.ALREADY_EXISTS, 'order exists'],
]) {
  test(`${scenario}: the error leaves the span (the helper marks and records it), metrics carry error.type, constant gRPC error`, async () => {
    const d = deps(okDb());
    const out = await placeOrderInSpan(d, request);
    assert.deepEqual(out, { error: { code, details } });
    assert.equal(d.calls[0].thrown.name, type, 'the failure is thrown out of withSpan, not swallowed');
    assert.deepEqual(d.logs, []);
    const attrs = { 'demo.scenario': scenario, 'demo.outcome': 'error', 'error.type': type };
    assert.deepEqual(d.points.map((p) => [p.metric, p.a, p.inside]), [['duration', attrs, true], ['processed', attrs, true]]);
    assert.ok(!JSON.stringify(out).includes('-r1'), 'no run token in the gRPC answer');
  });
}

test('unexpected failure: INTERNAL order failed, error.type = the helper value of the error', async () => {
  const db = { ...okDb(), async upsert() { throw new TypeError('socket closed'); } };
  const d = deps(db);
  const out = await placeOrderInSpan(d, { order_id: 'ok-2', customer: 'c', amount: 11, scenario: 'ok' });
  assert.deepEqual(out, { error: { code: CODE.INTERNAL, details: 'order failed' } });
  assert.equal(d.points[1].a['error.type'], 'TypeError');
});

test('unknown scenario: INVALID_ARGUMENT, no span, no metric', async () => {
  const d = deps(okDb());
  const out = await placeOrderInSpan(d, { order_id: 'x', scenario: 'unhandled' });
  assert.deepEqual(out, { error: { code: CODE.INVALID_ARGUMENT, details: 'invalid scenario' } });
  assert.equal(d.calls.length, 0);
  assert.equal(d.points.length, 0);
});

test('without deps.now the clock is performance.now()', async () => {
  const d = deps(okDb(), { now: undefined });
  await placeOrderInSpan(d, { order_id: 'ok-3', customer: 'c', amount: 11, scenario: 'ok' });
  assert.ok(d.points[0].v >= 0);
});
