'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const { AMOUNT_LIMIT, CUSTOMERS, CYCLE, ROW_SLOTS, buildOrder, scenarioFor } = require('../lib/scenarios');

test('the cycle has the DE-D7 positions', () => {
  // Positions are 1-based in the spec.
  const at = (pos) => CYCLE[pos - 1];
  for (const pos of [1, 2, 4, 5, 7, 8, 10]) assert.equal(at(pos), 'ok');
  assert.equal(at(3), 'business_error');
  assert.equal(at(6), 'db_error');
  assert.equal(at(9), 'unhandled');
  assert.equal(CYCLE.length, 10);
});

test('scenarioFor is 1-based and repeats the cycle (zero and negatives too)', () => {
  assert.equal(scenarioFor(1), 'ok');
  assert.equal(scenarioFor(3), 'business_error');
  assert.equal(scenarioFor(6), 'db_error');
  assert.equal(scenarioFor(9), 'unhandled');
  assert.equal(scenarioFor(13), 'business_error');
  assert.equal(scenarioFor(0), 'ok');
  assert.equal(scenarioFor(-1), 'unhandled');
});

test('ok orders upsert ok-<seq mod 500>', () => {
  assert.equal(buildOrder(1).order_id, 'ok-1');
  assert.equal(buildOrder(ROW_SLOTS + 1).order_id, 'ok-1');
  assert.equal(buildOrder(3).order_id, 'ord-3');
});

test('error orders have ids without any run token', () => {
  for (const seq of [3, 6, 9]) {
    const order = buildOrder(seq);
    assert.match(order.order_id, /^ord-\d+$/);
  }
});

test('business_error exceeds the limit; other scenarios stay below it', () => {
  for (let seq = 1; seq <= 200; seq += 1) {
    const order = buildOrder(seq);
    assert.equal(order.scenario, scenarioFor(seq));
    if (order.scenario === 'business_error') assert.equal(order.amount, 5000);
    else {
      assert.equal(order.amount, 10 + (seq % 90));
      assert.ok(order.amount <= AMOUNT_LIMIT);
    }
  }
});

test('customers cycle over three values', () => {
  assert.deepEqual([...CUSTOMERS], ['cust-a', 'cust-b', 'cust-c']);
  const seen = new Set([1, 2, 3].map((seq) => buildOrder(seq).customer));
  assert.deepEqual([...seen].sort(), [...CUSTOMERS].sort());
  assert.equal(buildOrder(4).customer, buildOrder(1).customer);
});
