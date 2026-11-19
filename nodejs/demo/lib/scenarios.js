// The worker's deterministic scenario cycle and request bodies
// (specs/demo-environment DE-D7), and the business limit the backend applies.
'use strict';

/** Cycle of 10, positions 1..10: ok at 1,2,4,5,7,8,10; business_error 3; db_error 6; unhandled 9. */
const CYCLE = Object.freeze([
  'ok', 'ok', 'business_error', 'ok', 'ok', 'db_error', 'ok', 'ok', 'unhandled', 'ok',
]);

const SCENARIOS = Object.freeze(['ok', 'business_error', 'db_error', 'unhandled']);

/** Three customers, so the api's quote cache both hits and misses. */
const CUSTOMERS = Object.freeze(['cust-a', 'cust-b', 'cust-c']);

/** Orders above this amount are rejected by the backend (business_error). */
const AMOUNT_LIMIT = 1000;

/** Rows kept per language: ok orders upsert ok-<seq mod ROW_SLOTS> (DE-D4). */
const ROW_SLOTS = 500;

/** The scenario of request number `seq` (1-based: request 1 is cycle position 1, DE-D7 r3.5). */
function scenarioFor(seq) {
  const n = CYCLE.length;
  return CYCLE[(((seq - 1) % n) + n) % n];
}

/**
 * The POST /orders body of request `seq` (1-based); `ok` amounts are
 * 10 + seq mod 90 (DE-D7 r3.5). Order ids never carry the run id:
 * they are span attributes (order.id), and the run tokens must appear only in
 * the error messages (DE-D8, DR12).
 */
function buildOrder(seq) {
  const scenario = scenarioFor(seq);
  const slot = seq % ROW_SLOTS;
  return {
    order_id: scenario === 'ok' ? `ok-${slot}` : `ord-${slot}`,
    customer: CUSTOMERS[seq % CUSTOMERS.length],
    amount: scenario === 'business_error' ? AMOUNT_LIMIT * 5 : 10 + (seq % 90),
    scenario,
  };
}

module.exports = { AMOUNT_LIMIT, CUSTOMERS, CYCLE, ROW_SLOTS, SCENARIOS, buildOrder, scenarioFor };
