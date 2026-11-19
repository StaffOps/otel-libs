// The api's request validation and the backend's order processing
// (specs/demo-environment DE-D4, design "Scenario table"), free of I/O so
// the scenarios are unit-tested with a fake database.
'use strict';

const { OrderExistsError, OrderRejectedError } = require('./errors');
const { AMOUNT_LIMIT, SCENARIOS } = require('./scenarios');

/** Upsert of one order; every value a bind parameter (DR1). */
const UPSERT_SQL = 'INSERT INTO orders (id, customer, amount, scenario, updated_at) VALUES ($1, $2, $3, $4, now()) '
  + 'ON CONFLICT (id) DO UPDATE SET customer = EXCLUDED.customer, amount = EXCLUDED.amount, '
  + 'scenario = EXCLUDED.scenario, updated_at = EXCLUDED.updated_at';

/** db_error: a plain insert (not the upsert), so the BEFORE INSERT trigger raises 23505 (DE-D7 r3.5). */
const INSERT_SQL = 'INSERT INTO orders (id, customer, amount, scenario, updated_at) VALUES ($1, $2, $3, $4, now())';

/** Read-back of one order. */
const SELECT_SQL = 'SELECT id, customer, amount, scenario, updated_at FROM orders WHERE id = $1';

/** PostgreSQL unique_violation, raised by the demo trigger for dup- ids (demo/sql/schema.sql). */
const UNIQUE_VIOLATION = '23505';

const isPlainObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Scenarios the backend accepts (`unhandled` fails in the api before the call). */
const BACKEND_SCENARIOS = Object.freeze(['ok', 'business_error', 'db_error']);

/** True when the backend accepts the request's scenario; else INVALID_ARGUMENT (DE-D7 r3.5). */
function isBackendScenario(scenario) {
  return BACKEND_SCENARIOS.includes(scenario);
}

/** Validates the POST /orders body {"order_id", "customer", "amount", "scenario"}; `{order}` or `{error}`. */
function parseOrderBody(body) {
  if (!isPlainObject(body)) return { error: 'body: must be a JSON object' };
  const { order_id: orderId, customer, amount, scenario } = body;
  if (typeof orderId !== 'string' || orderId === '' || orderId.length > 64) return { error: 'order_id: string of 1-64 characters required' };
  if (typeof customer !== 'string' || customer === '' || customer.length > 64) return { error: 'customer: string of 1-64 characters required' };
  if (typeof amount !== 'number' || !Number.isFinite(amount) || amount < 0) return { error: 'amount: non-negative number required' };
  if (!SCENARIOS.includes(scenario)) return { error: `scenario: one of ${SCENARIOS.join(', ')}` };
  return { order: { order_id: orderId, customer, amount, scenario } };
}

/**
 * Processes one PlaceOrder request. `db` has `upsert(id, customer, amount,
 * scenario)`, `insert(...)` (same arguments) and `select(id)`. business_error: the amount limit rejects it
 * (OrderRejectedError, run token rej-<run>). db_error: the row dup-<run> is
 * inserted and the trigger's 23505 becomes OrderExistsError (run token
 * dup-<run>, the driver error as `driverError`). Other database errors propagate.
 * Returns the stored row.
 */
async function processOrder(request, run, db) {
  if (request.amount > AMOUNT_LIMIT) throw new OrderRejectedError(`amount over limit rej-${run}`);
  if (request.scenario === 'db_error') {
    const id = `dup-${run}`;
    try {
      await db.insert(id, request.customer, request.amount, request.scenario);
    } catch (err) {
      if (err && err.code === UNIQUE_VIOLATION) throw new OrderExistsError(`order ${id} exists`, err);
      throw err;
    }
    // Unreachable with the demo schema (the trigger raises for every dup- id).
    throw new Error('duplicate insert unexpectedly succeeded');
  }
  const id = request.order_id;
  await db.upsert(id, request.customer, request.amount, request.scenario);
  const row = await db.select(id);
  if (row === undefined) throw new Error('order not readable after upsert');
  return row;
}

/** A database row as the proto's Order message. */
function toOrderMessage(row) {
  const updated = row.updated_at instanceof Date ? row.updated_at.toISOString() : String(row.updated_at ?? '');
  return {
    order_id: row.id,
    customer: row.customer ?? '',
    amount: Number(row.amount ?? 0),
    scenario: row.scenario ?? '',
    updated_at: updated,
  };
}

module.exports = { BACKEND_SCENARIOS, INSERT_SQL, isBackendScenario, SELECT_SQL, UNIQUE_VIOLATION, UPSERT_SQL, parseOrderBody, processOrder, toOrderMessage };
