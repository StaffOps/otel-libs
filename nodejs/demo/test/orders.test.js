'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const { OrderExistsError, OrderRejectedError } = require('../lib/errors');
const { BACKEND_SCENARIOS, INSERT_SQL, SELECT_SQL, UNIQUE_VIOLATION, UPSERT_SQL, isBackendScenario, parseOrderBody, processOrder, toOrderMessage } = require('../lib/orders');

const VALID = { order_id: 'ok-1', customer: 'cust-a', amount: 41.5, scenario: 'ok' };

/** A fake db: rows in a Map; dup- ids raise the trigger's 23505 as pg does. */
function fakeDb({ failWith } = {}) {
  const rows = new Map();
  const calls = [];
  return {
    rows,
    calls,
    async upsert(id, customer, amount, scenario) {
      calls.push(['upsert', id]);
      if (failWith) throw failWith;
      if (id.startsWith('dup-')) throw Object.assign(new Error(`duplicate order ${id}`), { code: UNIQUE_VIOLATION });
      rows.set(id, { id, customer, amount: String(amount), scenario, updated_at: new Date(0) });
    },
    async insert(id) {
      calls.push(['insert', id]);
      if (failWith) throw failWith;
      if (id.startsWith('dup-')) throw Object.assign(new Error(`duplicate order ${id}`), { code: UNIQUE_VIOLATION });
    },
    async select(id) {
      calls.push(['select', id]);
      return rows.get(id);
    },
  };
}

test('statements use bind parameters only', () => {
  assert.match(UPSERT_SQL, /VALUES \(\$1, \$2, \$3, \$4, now\(\)\)/);
  assert.match(UPSERT_SQL, /ON CONFLICT \(id\) DO UPDATE/);
  assert.match(SELECT_SQL, /WHERE id = \$1$/);
  assert.match(INSERT_SQL, /^INSERT INTO orders .* VALUES \(\$1, \$2, \$3, \$4, now\(\)\)$/);
  assert.doesNotMatch(INSERT_SQL, /ON CONFLICT/);
});

test('parseOrderBody accepts a valid body', () => {
  assert.deepEqual(parseOrderBody(VALID), { order: VALID });
  assert.deepEqual(parseOrderBody({ ...VALID, extra: 1 }), { order: VALID });
});

test('parseOrderBody rejects each invalid field', () => {
  const cases = [
    [null, /body/], [[], /body/], ['x', /body/],
    [{ ...VALID, order_id: '' }, /order_id/], [{ ...VALID, order_id: 'x'.repeat(65) }, /order_id/], [{ ...VALID, order_id: 1 }, /order_id/],
    [{ ...VALID, customer: '' }, /customer/], [{ ...VALID, customer: 'x'.repeat(65) }, /customer/], [{ ...VALID, customer: null }, /customer/],
    [{ ...VALID, amount: '1' }, /amount/], [{ ...VALID, amount: -1 }, /amount/], [{ ...VALID, amount: Number.NaN }, /amount/],
    [{ ...VALID, scenario: 'other' }, /scenario/],
  ];
  for (const [body, re] of cases) assert.match(parseOrderBody(body).error, re, JSON.stringify(body));
});

test('ok: upsert then select of the request id', async () => {
  const db = fakeDb();
  const row = await processOrder(VALID, 'r1', db);
  assert.equal(row.id, 'ok-1');
  assert.deepEqual(db.calls, [['upsert', 'ok-1'], ['select', 'ok-1']]);
});

test('business_error: OrderRejectedError with the rej- run token, no database call', async () => {
  const db = fakeDb();
  await assert.rejects(processOrder({ ...VALID, scenario: 'business_error', amount: 5000 }, 'r1', db), (err) => {
    assert.ok(err instanceof OrderRejectedError);
    assert.equal(err.message, 'amount over limit rej-r1');
    return true;
  });
  assert.deepEqual(db.calls, []);
});

test('db_error: plain insert of dup-<run>; the 23505 becomes OrderExistsError', async () => {
  const db = fakeDb();
  await assert.rejects(processOrder({ ...VALID, order_id: 'ord-6', scenario: 'db_error' }, 'r1', db), (err) => {
    assert.ok(err instanceof OrderExistsError);
    assert.equal(err.message, 'order dup-r1 exists');
    assert.equal(err.driverError.message, 'duplicate order dup-r1');
    return true;
  });
  assert.deepEqual(db.calls, [['insert', 'dup-r1']]);
});

test('db_error: other insert errors propagate; a successful dup insert is an error', async () => {
  const boom = new Error('connection refused');
  await assert.rejects(processOrder({ ...VALID, scenario: 'db_error' }, 'r1', fakeDb({ failWith: boom })), (err) => err === boom);
  const db = fakeDb();
  db.insert = async () => {};
  await assert.rejects(processOrder({ ...VALID, scenario: 'db_error' }, 'r1', db), /duplicate insert unexpectedly succeeded/);
});

test('isBackendScenario: ok, business_error, db_error only', () => {
  assert.deepEqual([...BACKEND_SCENARIOS], ['ok', 'business_error', 'db_error']);
  for (const s of BACKEND_SCENARIOS) assert.equal(isBackendScenario(s), true);
  for (const s of ['unhandled', '', 'x', undefined]) assert.equal(isBackendScenario(s), false);
});

test('other database errors propagate unchanged', async () => {
  const boom = Object.assign(new Error('connection refused'), { code: 'ECONNREFUSED' });
  await assert.rejects(processOrder(VALID, 'r1', fakeDb({ failWith: boom })), (err) => err === boom);
});

test('a row that cannot be read back is an error', async () => {
  const db = fakeDb();
  db.select = async () => undefined;
  await assert.rejects(processOrder(VALID, 'r1', db), /order not readable after upsert/);
});

test('toOrderMessage maps a row to the proto Order', () => {
  assert.deepEqual(toOrderMessage({ id: 'ok-1', customer: 'c', amount: '41.5', scenario: 'ok', updated_at: new Date(0) }), {
    order_id: 'ok-1', customer: 'c', amount: 41.5, scenario: 'ok', updated_at: '1970-01-01T00:00:00.000Z',
  });
  assert.deepEqual(toOrderMessage({ id: 'x', updated_at: '2026-01-01' }), {
    order_id: 'x', customer: '', amount: 0, scenario: '', updated_at: '2026-01-01',
  });
  assert.equal(toOrderMessage({ id: 'y' }).updated_at, '');
});
