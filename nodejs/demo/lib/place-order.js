// The backend's PlaceOrder handling in the owning span demo.order.process
// (specs/demo-environment DE-D8, DE-D9), with its collaborators injected so
// the failure path is unit-tested with a fake withSpan (the unit tests install
// the published helper pins, which predate A45).
'use strict';

const { errorTypeOf } = require('./failure');
const { CODE, grpcErrorFor } = require('./grpc-status');
const { isBackendScenario, processOrder } = require('./orders');

/**
 * PlaceOrder: an unknown scenario is a caller error (INVALID_ARGUMENT, no
 * span, DE-D7 r3.5); otherwise the order is processed inside
 * `deps.withSpan('demo.order.process', …)` (the helper's A45 span function,
 * contract design D15): a failure leaves the span, so the helper marks it and
 * writes the one ERROR record, and the gRPC answer — a constant message — is
 * built after the span ended. The custom metrics are recorded inside the span
 * (trace_based exemplars) with `demo.outcome` and, on failure only,
 * `error.type` = the value the helper puts on the span.
 *
 * @param {object} deps  `{ withSpan, tracer, db, run, log, processed, duration, now? }`
 * @param {object} request  the PlaceOrderRequest message
 * @returns {Promise<{ reply?: object, error?: { code: number, details: string } }>}
 */
async function placeOrderInSpan(deps, request) {
  if (!isBackendScenario(request.scenario)) return { error: { code: CODE.INVALID_ARGUMENT, details: 'invalid scenario' } };
  const now = deps.now || (() => performance.now());
  try {
    return await deps.withSpan('demo.order.process', async () => {
      const started = now();
      let attrs = { 'demo.scenario': request.scenario, 'demo.outcome': 'ok' };
      try {
        const row = await processOrder(request, deps.run, deps.db);
        deps.log.info({ 'order.id': row.id, 'demo.scenario': request.scenario }, 'order processed');
        return { reply: { order_id: row.id, status: 'processed' } };
      } catch (err) {
        attrs = { ...attrs, 'demo.outcome': 'error', 'error.type': errorTypeOf(err) };
        throw err;
      } finally {
        deps.duration.record((now() - started) / 1000, attrs);
        deps.processed.add(1, attrs);
      }
    }, { tracer: deps.tracer, attributes: { 'demo.scenario': request.scenario, 'order.id': request.order_id } });
  } catch (err) {
    return { error: grpcErrorFor(err) };
  }
}

module.exports = { placeOrderInSpan };
